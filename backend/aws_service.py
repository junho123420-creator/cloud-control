"""Boto3 조회 로직. FastAPI에 의존하지 않고 오류는 AwsServiceError로 변환한다."""

import boto3
from botocore import exceptions as bex
from botocore.config import Config

from backend.config import Settings

BOTO_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"max_attempts": 2, "mode": "standard"},
)

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


class AwsServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


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
                })
        return items

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
