"""Boto3 조회 로직. FastAPI에 의존하지 않고 오류는 AwsServiceError로 변환한다."""

from datetime import datetime, timezone

import boto3
from botocore import exceptions as bex
from botocore.config import Config

from backend.config import Settings
from backend.errors import AppError
from backend.security_check import inspect_group

BOTO_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
)
# 변경(시작·중지) 전용: 결과가 불확실할 때 SDK가 몰래 재시도하지 않도록 한 번만 시도한다.
CONTROL_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"total_max_attempts": 1, "mode": "standard"},
)

# 보안 그룹 점검: 실제 EC2(종료되지 않은 인스턴스)에 연결된 그룹만 대상으로 한다.
SCAN_INSTANCE_STATES = ["pending", "running", "stopping", "stopped"]
SG_BATCH_SIZE = 100

# 로그인 만료·자격증명 없음·갱신 불가
LOGIN_EXCEPTIONS = (
    bex.NoCredentialsError,
    bex.PartialCredentialsError,
    bex.ProfileNotFound,
    bex.LoginError,
    bex.TokenRetrievalError,
    bex.SSOTokenLoadError,
    bex.UnauthorizedSSOTokenError,
    bex.CredentialRetrievalError,
    bex.RefreshWithMFAUnsupportedError,
)
LOGIN_ERROR_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "AuthFailure",
    "RequestExpired",
    "InvalidToken",
}
ACCESS_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AllAccessDisabled",
}


class AwsServiceError(AppError):
    pass


def _translate(exc: Exception) -> AwsServiceError:
    if isinstance(exc, LOGIN_EXCEPTIONS):
        return AwsServiceError(
            503, "AWS_LOGIN_REQUIRED",
            "AWS 로그인이 필요하거나 만료되었습니다. 터미널에서 aws login --profile <프로필>을 실행하세요.",
        )
    if isinstance(exc, bex.ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in LOGIN_ERROR_CODES:
            return _translate(bex.NoCredentialsError())
        if code in ACCESS_DENIED_CODES:
            return AwsServiceError(403, "AWS_ACCESS_DENIED", "AWS 권한이 부족해 요청이 거부되었습니다.")
        return AwsServiceError(502, "AWS_REQUEST_FAILED", f"AWS 요청이 실패했습니다. ({code or '알 수 없는 오류'})")
    # EndpointConnectionError는 ConnectionError, 타임아웃은 HTTPClientError 계열이다.
    if isinstance(exc, (bex.ConnectionError, bex.HTTPClientError)):
        return AwsServiceError(503, "AWS_CONNECTION_ERROR", "AWS와 통신하지 못했습니다. 네트워크 상태를 확인하세요.")
    return AwsServiceError(502, "AWS_REQUEST_FAILED", "AWS 요청 처리 중 오류가 발생했습니다.")


# 변경 호출 결과 분류
OUTCOME_FAILED = "FAILED"  # AWS가 명시적으로 거절했거나 요청이 전송되지 않음
OUTCOME_UNKNOWN = "UNKNOWN"  # 전송 후 응답을 받지 못해 결과를 알 수 없음


def classify_control_error(exc: Exception) -> tuple[str, AwsServiceError]:
    """시작·중지 호출 실패를 (FAILED|UNKNOWN, 응답 오류)로 분류한다."""
    unknown = AwsServiceError(
        503, "AWS_RESULT_UNKNOWN",
        "AWS 응답을 받지 못해 결과를 알 수 없습니다. 콘솔에서 인스턴스 상태를 확인하세요. 자동으로 다시 요청하지 않습니다.",
    )
    # 연결 자체가 안 됐으면 요청이 전송되지 않은 것이다. (ConnectTimeoutError는 HTTPClientError이기도 하다)
    if isinstance(exc, (bex.ConnectTimeoutError, bex.EndpointConnectionError)):
        return OUTCOME_FAILED, _translate(exc)
    if isinstance(exc, LOGIN_EXCEPTIONS):
        return OUTCOME_FAILED, _translate(exc)
    if isinstance(exc, bex.ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if status >= 500:
            return OUTCOME_UNKNOWN, unknown
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "IncorrectInstanceState":
            return OUTCOME_FAILED, AwsServiceError(409, "INVALID_STATE", "AWS가 현재 인스턴스 상태에서 이 작업을 거절했습니다.")
        return OUTCOME_FAILED, _translate(exc)
    # 읽기 timeout, 연결 끊김, 그 밖의 예외는 전송 후일 수 있으므로 불확실로 본다.
    return OUTCOME_UNKNOWN, unknown


def _scan_incomplete(reason: str) -> AwsServiceError:
    return AwsServiceError(
        503, "AWS_SCAN_INCOMPLETE",
        f"보안 그룹 점검을 완료하지 못했습니다: {reason} 잠시 후 다시 점검하세요.",
    )


class AwsService:
    def __init__(self, settings: Settings):
        self.settings = settings
        # 프로필이 없으면 Session 생성 시점에 ProfileNotFound가 발생한다.
        self.session = self._call(
            boto3.Session,
            profile_name=settings.aws_profile,
            region_name=settings.aws_region,
        )

    def _client(self, name: str):
        return self.session.client(name, config=BOTO_CONFIG)

    def _control_client(self):
        return self.session.client("ec2", config=CONTROL_CONFIG)

    def _call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AwsServiceError:
            raise
        except (bex.BotoCoreError, bex.ClientError) as exc:
            raise _translate(exc) from None

    def get_context(self) -> dict:
        """STS 호출로 계정을 확인한다. 예상 계정이 아니면 403으로 차단한다."""
        identity = self._call(self._client("sts").get_caller_identity)
        if identity["Account"] != self.settings.expected_account_id:
            raise AwsServiceError(
                403, "AWS_ACCOUNT_MISMATCH",
                "로그인한 AWS 계정이 EXPECTED_ACCOUNT_ID와 다릅니다. 프로필과 설정을 확인하세요.",
            )
        return {
            "account_id": identity["Account"],
            "caller_arn": identity["Arn"],
            "region": self.settings.aws_region,
        }

    def _paginate(self, service: str, operation: str, key: str) -> list:
        def run():
            paginator = self._client(service).get_paginator(operation)
            return [item for page in paginator.paginate() for item in page.get(key, [])]

        return self._call(run)

    def list_ec2_instances(self) -> list[dict]:
        reservations = self._paginate("ec2", "describe_instances", "Reservations")
        items = []
        for reservation in reservations:
            for inst in reservation.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                items.append({
                    "instance_id": inst["InstanceId"],
                    "name": tags.get("Name") or inst["InstanceId"],
                    "state": inst.get("State", {}).get("Name"),
                    "instance_type": inst.get("InstanceType"),
                    "private_ip": inst.get("PrivateIpAddress"),
                    "public_ip": inst.get("PublicIpAddress"),
                    "controllable": inst["InstanceId"] in self.settings.allowed_instance_ids,
                })
        return items

    def ensure_controllable(self, instance_id: str) -> None:
        """제어 요청을 AWS 호출 전에 서버 허용 목록으로 검사한다."""
        allowed = self.settings.allowed_instance_ids
        if not allowed:
            raise AwsServiceError(
                403, "CONTROL_DISABLED",
                "제어 허용 목록(ALLOWED_INSTANCE_IDS)이 비어 있어 시작·중지를 할 수 없습니다.",
            )
        if instance_id not in allowed:
            raise AwsServiceError(403, "INSTANCE_NOT_ALLOWED", "제어 허용 목록에 없는 인스턴스입니다.")

    def get_instance_state(self, instance_id: str) -> str:
        """최신 상태를 AWS에서 직접 읽는다 (조회용 client)."""

        def run():
            try:
                resp = self._client("ec2").describe_instances(InstanceIds=[instance_id])
            except bex.ClientError as exc:
                if exc.response.get("Error", {}).get("Code", "").startswith("InvalidInstanceID"):
                    raise AwsServiceError(404, "INSTANCE_NOT_FOUND", "인스턴스를 찾을 수 없습니다.") from None
                raise
            for reservation in resp.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    if inst.get("InstanceId") == instance_id:
                        return inst.get("State", {}).get("Name", "")
            raise AwsServiceError(404, "INSTANCE_NOT_FOUND", "인스턴스를 찾을 수 없습니다.")

        return self._call(run)

    def change_instance_state(self, instance_id: str, action: str) -> str | None:
        """시작·중지를 한 번만 호출하고 AWS RequestId를 반환한다.

        예외는 변환하지 않고 그대로 올려 호출자가 FAILED/UNKNOWN을 판단하게 한다.
        강제 중지·종료·waiter는 사용하지 않는다.
        """
        client = self._control_client()
        if action == "start":
            resp = client.start_instances(InstanceIds=[instance_id])
        elif action == "stop":
            resp = client.stop_instances(InstanceIds=[instance_id])
        else:
            raise ValueError("지원하지 않는 작업")
        return resp.get("ResponseMetadata", {}).get("RequestId")

    # ---- 보안 그룹 점검 (DescribeInstances, DescribeSecurityGroups 조회만 사용) ----

    def collect_group_attachments(self) -> dict[str, list[dict]]:
        """그룹 ID → 연결 목록. 연결은 (instance_id, network_interface_id, group_id)로 중복 제거한다.

        제어 허용 목록(ALLOWED_INSTANCE_IDS)은 시작·중지 제한이므로 여기서는 쓰지 않는다.
        """

        def run():
            paginator = self._client("ec2").get_paginator("describe_instances")
            pages = paginator.paginate(
                Filters=[{"Name": "instance-state-name", "Values": SCAN_INSTANCE_STATES}]
            )
            attachments: dict[str, list[dict]] = {}
            seen: set[tuple[str, str, str]] = set()
            for page in pages:
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        instance_id = inst.get("InstanceId")
                        interfaces = inst.get("NetworkInterfaces")
                        # 실행 중인 EC2는 ENI와 보안 그룹을 반드시 가진다. 없으면 0건으로 단정하지 않는다.
                        if not instance_id or not interfaces:
                            raise _scan_incomplete("인스턴스의 네트워크 인터페이스 정보가 누락되었습니다.")
                        tags = {t.get("Key"): t.get("Value") for t in inst.get("Tags", [])}
                        name = tags.get("Name") or instance_id
                        state = inst.get("State", {}).get("Name")
                        for eni in interfaces:
                            eni_id = eni.get("NetworkInterfaceId")
                            groups = eni.get("Groups")
                            if not eni_id or not groups:
                                raise _scan_incomplete(f"{instance_id}의 보안 그룹 정보가 누락되었습니다.")
                            for group in groups:
                                group_id = group.get("GroupId")
                                if not group_id:
                                    raise _scan_incomplete(f"{instance_id}의 보안 그룹 ID가 누락되었습니다.")
                                key = (instance_id, eni_id, group_id)
                                if key in seen:
                                    continue
                                seen.add(key)
                                attachments.setdefault(group_id, []).append({
                                    "instance_id": instance_id,
                                    "instance_name": name,
                                    "state": state,
                                    "network_interface_id": eni_id,
                                })
            return attachments

        return self._call(run)

    def describe_groups_by_id(self, group_ids) -> dict[str, dict]:
        """정렬한 고유 ID를 100개씩 묶어 조회한다. 요청한 그룹이 하나라도 빠지면 점검 실패다."""
        ids = sorted(set(group_ids))
        if not ids:
            # 빈 GroupIds로 호출하면 계정의 모든 보안 그룹이 조회되므로 호출하지 않는다.
            return {}
        client = self._client("ec2")
        found: dict[str, dict] = {}
        for start in range(0, len(ids), SG_BATCH_SIZE):
            batch = ids[start:start + SG_BATCH_SIZE]

            def run(batch=batch):
                try:
                    return client.describe_security_groups(GroupIds=batch)
                except bex.ClientError as exc:
                    if exc.response.get("Error", {}).get("Code", "").startswith("InvalidGroup"):
                        raise _scan_incomplete("조회 중 보안 그룹이 삭제되었거나 찾을 수 없습니다.") from None
                    raise

            groups = self._call(run).get("SecurityGroups", [])
            returned = {g.get("GroupId") for g in groups}
            if returned != set(batch):
                raise _scan_incomplete("요청한 보안 그룹과 조회 결과가 일치하지 않습니다.")
            for g in groups:
                found[g["GroupId"]] = g
        return found

    def scan_security_groups(self) -> dict:
        """실제 EC2 ENI에 연결된 보안 그룹의 공개 TCP 22·3389 규칙을 점검한다.

        모든 조회가 성공했을 때만 결과를 반환한다. 중간 실패는 예외로 올라가며 "발견 없음"이 되지 않는다.
        finding은 (그룹 인바운드 규칙, 공개 CIDR) 단위이며 그룹을 쓰는 인스턴스 수만큼 늘리지 않는다.
        """
        self.get_context()  # 예상 계정이 아니면 403
        connections = self.collect_group_attachments()
        groups = self.describe_groups_by_id(connections.keys())

        checked_groups = []
        findings = []
        for group_id in sorted(groups):
            group = groups[group_id]  # 원본 GroupId/GroupName/IpPermissions 구조 그대로
            group_connections = connections[group_id]
            for finding in inspect_group(group):  # 그룹마다 한 번만 전달
                findings.append(finding | {"connections": group_connections})
            checked_groups.append({
                "group_id": group_id,
                "group_name": group.get("GroupName", ""),
                "connections": group_connections,
            })

        instance_ids = {c["instance_id"] for conns in connections.values() for c in conns}
        return {
            "findings": findings,
            "checked_groups": checked_groups,
            "checked_instance_count": len(instance_ids),
            "checked_group_count": len(checked_groups),
            "finding_count": len(findings),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }

    def list_s3_buckets(self) -> list[dict]:
        buckets = self._paginate("s3", "list_buckets", "Buckets")
        return [{"name": b["Name"]} for b in buckets]

    def list_security_groups(self) -> list[dict]:
        groups = self._paginate("ec2", "describe_security_groups", "SecurityGroups")
        return [
            {
                "GroupId": g["GroupId"],
                "GroupName": g["GroupName"],
                "IpPermissions": g.get("IpPermissions", []),
            }
            for g in groups
        ]
