"""보안 그룹 점검 수집 로직 테스트. 실제 AWS를 호출하지 않는다.

실행: python -m unittest -v tests.test_security_scan
"""

import unittest
from unittest import mock

from botocore import exceptions as bex

from backend import aws_service
from backend.aws_service import AwsService, AwsServiceError
from backend.config import Settings

ACCOUNT = "111111111111"


def settings(allowed=frozenset()):
    return Settings(
        aws_profile="test", aws_region="ap-northeast-2", expected_account_id=ACCOUNT,
        admin_api_token="t", allowed_instance_ids=allowed,
    )


def instance(iid, enis, name=None, state="running"):
    inst = {"InstanceId": iid, "State": {"Name": state}, "NetworkInterfaces": enis}
    if name:
        inst["Tags"] = [{"Key": "Name", "Value": name}]
    return inst


def eni(eni_id, *group_ids):
    return {"NetworkInterfaceId": eni_id, "Groups": [{"GroupId": g, "GroupName": g} for g in group_ids]}


def sg(group_id, *perms):
    return {"GroupId": group_id, "GroupName": f"name-{group_id}", "IpPermissions": list(perms)}


SSH_OPEN = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}], "Ipv6Ranges": []}
DB_PRIVATE = {"IpProtocol": "tcp", "FromPort": 3306, "ToPort": 3306, "IpRanges": [{"CidrIp": "10.0.0.0/16"}], "Ipv6Ranges": []}


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.pages = []
        self.groups = {}
        self.account = ACCOUNT
        self.ec2 = mock.MagicMock(name="ec2")
        paginator = mock.MagicMock()
        paginator.paginate.side_effect = lambda **kw: iter(self.pages)
        self.paginator = paginator
        self.ec2.get_paginator.return_value = paginator
        self.ec2.describe_security_groups.side_effect = lambda GroupIds: {
            "SecurityGroups": [self.groups[g] for g in GroupIds if g in self.groups]
        }
        sts = mock.MagicMock()
        sts.get_caller_identity.side_effect = lambda: {"Account": self.account, "Arn": "arn:x"}
        session = mock.MagicMock()
        session.client.side_effect = lambda name, config=None: sts if name == "sts" else self.ec2
        p = mock.patch.object(aws_service.boto3, "Session", return_value=session)
        p.start()
        self.addCleanup(p.stop)

    def scan(self, allowed=frozenset()):
        return AwsService(settings(allowed)).scan_security_groups()

    def assert_error(self, status, code):
        with self.assertRaises(AwsServiceError) as ctx:
            self.scan()
        self.assertEqual((ctx.exception.status_code, ctx.exception.code), (status, code))
        return ctx.exception

    # ---- 수집 ----

    def test_state_filter_is_applied(self):
        self.scan()
        self.ec2.get_paginator.assert_called_with("describe_instances")
        filters = self.paginator.paginate.call_args.kwargs["Filters"]
        self.assertEqual(filters, [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])

    def test_collects_all_enis_and_dedupes(self):
        self.pages = [
            {"Reservations": [{"Instances": [
                instance("i-1", [eni("eni-a", "sg-1", "sg-2"), eni("eni-b", "sg-2")], name="web"),
            ]}]},
            {"Reservations": [{"Instances": [
                instance("i-2", [eni("eni-c", "sg-1", "sg-1")], state="stopped"),  # Name 없음, 같은 그룹 중복
            ]}]},
        ]
        self.groups = {"sg-1": sg("sg-1", SSH_OPEN), "sg-2": sg("sg-2", DB_PRIVATE)}
        result = self.scan()

        by_id = {g["group_id"]: g for g in result["checked_groups"]}
        self.assertEqual(sorted(by_id), ["sg-1", "sg-2"])
        self.assertEqual(by_id["sg-1"]["connections"], [
            {"instance_id": "i-1", "instance_name": "web", "state": "running", "network_interface_id": "eni-a"},
            {"instance_id": "i-2", "instance_name": "i-2", "state": "stopped", "network_interface_id": "eni-c"},
        ])
        self.assertEqual(
            [a["network_interface_id"] for a in by_id["sg-2"]["connections"]], ["eni-a", "eni-b"]
        )
        self.assertEqual(
            (result["checked_instance_count"], result["checked_group_count"], result["finding_count"]), (2, 2, 1)
        )
        self.assertEqual(result["findings"][0]["group_id"], "sg-1")
        self.assertEqual(result["findings"][0]["connections"], by_id["sg-1"]["connections"])
        self.assertRegex(result["checked_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        # 고유 ID를 정렬해 한 번에 조회
        self.ec2.describe_security_groups.assert_called_once_with(GroupIds=["sg-1", "sg-2"])

    def test_each_group_is_inspected_once(self):
        self.pages = [{"Reservations": [{"Instances": [
            instance("i-1", [eni("eni-a", "sg-1")]), instance("i-2", [eni("eni-b", "sg-1")]),
        ]}]}]
        self.groups = {"sg-1": sg("sg-1", SSH_OPEN)}
        with mock.patch.object(aws_service, "inspect_group", wraps=aws_service.inspect_group) as spy:
            result = self.scan()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(len(result["checked_groups"][0]["connections"]), 2)
        # 같은 그룹을 두 인스턴스가 써도 finding은 1건
        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(len(result["findings"][0]["connections"]), 2)

    def test_batches_of_100_sorted(self):
        ids = [f"sg-{i:04d}" for i in range(150)]
        self.pages = [{"Reservations": [{"Instances": [
            instance(f"i-{i}", [eni(f"eni-{i}", gid)]) for i, gid in enumerate(reversed(ids))
        ]}]}]
        self.groups = {g: sg(g) for g in ids}
        result = self.scan()
        calls = [c.kwargs["GroupIds"] for c in self.ec2.describe_security_groups.call_args_list]
        self.assertEqual([len(c) for c in calls], [100, 50])
        self.assertEqual(calls[0] + calls[1], sorted(ids))
        self.assertEqual(result["checked_group_count"], 150)

    def test_allowlist_is_not_used_as_scan_filter(self):
        self.pages = [{"Reservations": [{"Instances": [
            instance("i-allowed", [eni("eni-a", "sg-1")]), instance("i-other", [eni("eni-b", "sg-2")]),
        ]}]}]
        self.groups = {"sg-1": sg("sg-1"), "sg-2": sg("sg-2")}
        result = self.scan(allowed=frozenset({"i-allowed"}))
        self.assertEqual((result["checked_group_count"], result["checked_instance_count"]), (2, 2))

    def test_finding_unit_is_rule_and_public_cidr(self):
        wide = {"IpProtocol": "tcp", "FromPort": 0, "ToPort": 65535,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}], "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}
        all_proto = {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}], "Ipv6Ranges": []}
        self.pages = [{"Reservations": [{"Instances": [
            instance("i-1", [eni("eni-a", "sg-1")]), instance("i-2", [eni("eni-b", "sg-1")]),
            instance("i-3", [eni("eni-c", "sg-2")]),
        ]}]}]
        self.groups = {"sg-1": sg("sg-1", wide), "sg-2": sg("sg-2", all_proto, DB_PRIVATE)}
        result = self.scan()
        # sg-1: 한 규칙 × (IPv4, IPv6) = 2건, 인스턴스가 2개여도 늘지 않음. sg-2: 전체 프로토콜 1건
        self.assertEqual(result["finding_count"], 3)
        self.assertEqual(result["finding_count"], len(result["findings"]))
        for f in result["findings"]:
            self.assertEqual(f["management_ports"], [22, 3389])
        self.assertEqual(sorted(f["ip_family"] for f in result["findings"] if f["group_id"] == "sg-1"), ["IPv4", "IPv6"])

    def test_original_group_structure_is_passed(self):
        self.pages = [{"Reservations": [{"Instances": [instance("i-1", [eni("eni-a", "sg-1")])]}]}]
        original = sg("sg-1", SSH_OPEN) | {"VpcId": "vpc-1", "IpPermissionsEgress": []}
        self.groups = {"sg-1": original}
        with mock.patch.object(aws_service, "inspect_group", return_value=[]) as spy:
            result = self.scan()
        spy.assert_called_once_with(original)
        self.assertEqual(result["checked_group_count"], 1)  # 발견이 없어도 점검한 그룹은 포함

    # ---- 빈 결과와 비정상 누락 ----

    def test_no_instances_returns_empty_without_group_lookup(self):
        self.pages = [{"Reservations": []}]
        result = self.scan()
        self.assertEqual(
            {k: v for k, v in result.items() if k != "checked_at"},
            {"findings": [], "checked_groups": [], "checked_instance_count": 0, "checked_group_count": 0, "finding_count": 0},
        )
        self.ec2.describe_security_groups.assert_not_called()

    def test_empty_ids_never_call_describe_all(self):
        svc = AwsService(settings())
        self.assertEqual(svc.describe_groups_by_id([]), {})
        self.ec2.describe_security_groups.assert_not_called()

    def test_missing_network_interfaces_is_incomplete(self):
        for inst in (
            {"InstanceId": "i-1", "State": {"Name": "running"}},  # 키 없음
            {"InstanceId": "i-1", "State": {"Name": "running"}, "NetworkInterfaces": []},
        ):
            self.pages = [{"Reservations": [{"Instances": [inst]}]}]
            self.assert_error(503, "AWS_SCAN_INCOMPLETE")
        self.ec2.describe_security_groups.assert_not_called()

    def test_missing_groups_is_incomplete(self):
        for bad_eni in (
            {"NetworkInterfaceId": "eni-a"},
            {"NetworkInterfaceId": "eni-a", "Groups": []},
            {"NetworkInterfaceId": "eni-a", "Groups": [{"GroupName": "x"}]},
        ):
            self.pages = [{"Reservations": [{"Instances": [instance("i-1", [bad_eni])]}]}]
            self.assert_error(503, "AWS_SCAN_INCOMPLETE")
        self.ec2.describe_security_groups.assert_not_called()

    # ---- 그룹 조회 불일치 ----

    def test_group_missing_from_response_is_incomplete(self):
        self.pages = [{"Reservations": [{"Instances": [instance("i-1", [eni("eni-a", "sg-1", "sg-2")])]}]}]
        self.groups = {"sg-1": sg("sg-1", SSH_OPEN)}  # sg-2 누락
        self.assert_error(503, "AWS_SCAN_INCOMPLETE")

    def test_group_deleted_during_scan_is_incomplete(self):
        self.pages = [{"Reservations": [{"Instances": [instance("i-1", [eni("eni-a", "sg-1")])]}]}]
        self.ec2.describe_security_groups.side_effect = bex.ClientError(
            {"Error": {"Code": "InvalidGroup.NotFound"}}, "DescribeSecurityGroups"
        )
        self.assert_error(503, "AWS_SCAN_INCOMPLETE")

    def test_failure_in_second_batch_fails_whole_scan(self):
        ids = [f"sg-{i:04d}" for i in range(101)]
        self.pages = [{"Reservations": [{"Instances": [instance("i-1", [eni("eni-a", *ids)])]}]}]
        self.groups = {g: sg(g, SSH_OPEN) for g in ids[:100]}  # 두 번째 묶음 누락
        self.assert_error(503, "AWS_SCAN_INCOMPLETE")

    # ---- AWS 오류 분류 ----

    def test_access_denied_is_403(self):
        self.paginator.paginate.side_effect = bex.ClientError(
            {"Error": {"Code": "UnauthorizedOperation"}}, "DescribeInstances"
        )
        self.assert_error(403, "AWS_ACCESS_DENIED")

    def test_login_expired_is_503(self):
        self.paginator.paginate.side_effect = bex.LoginRefreshRequired()
        self.assert_error(503, "AWS_LOGIN_REQUIRED")

    def test_network_failure_is_503(self):
        self.pages = [{"Reservations": [{"Instances": [instance("i-1", [eni("eni-a", "sg-1")])]}]}]
        self.ec2.describe_security_groups.side_effect = bex.EndpointConnectionError(endpoint_url="https://ec2")
        self.assert_error(503, "AWS_CONNECTION_ERROR")

    def test_account_mismatch_is_403_before_ec2_calls(self):
        self.account = "222222222222"
        self.assert_error(403, "AWS_ACCOUNT_MISMATCH")
        self.ec2.get_paginator.assert_not_called()
        self.ec2.describe_security_groups.assert_not_called()

    def test_only_describe_operations_are_used(self):
        self.pages = [{"Reservations": [{"Instances": [instance("i-1", [eni("eni-a", "sg-1")])]}]}]
        self.groups = {"sg-1": sg("sg-1", SSH_OPEN)}
        self.scan()
        used = {c[0] for c in self.ec2.method_calls}
        self.assertTrue(used <= {"get_paginator", "describe_security_groups"}, used)


if __name__ == "__main__":
    unittest.main()


class SecurityCheckApiTest(unittest.TestCase):
    """GET /api/security-checks: 인증과 오류 전달 확인."""

    def setUp(self):
        import os
        from pathlib import Path

        from backend import config
        from tests.test_actions import TEST_ENV

        for p in (
            mock.patch.object(config, "ENV_FILE", Path("/nonexistent/.env")),
            mock.patch.dict(os.environ, TEST_ENV),
            mock.patch.object(aws_service.boto3, "Session"),
        ):
            p.start()
            self.addCleanup(p.stop)
        config.get_settings.cache_clear()
        self.addCleanup(config.get_settings.cache_clear)
        scan = mock.patch.object(AwsService, "scan_security_groups")
        self.scan = scan.start()
        self.addCleanup(scan.stop)

    def get(self, headers):
        from tests.test_actions import call

        return call("GET", "/api/security-checks", headers)

    def test_requires_operator_token(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            status, body = self.get(headers)
            self.assertEqual((status, body["detail"]["code"]), (401, "APP_AUTH_REQUIRED"))
        self.scan.assert_not_called()

    def test_errors_are_not_reported_as_no_findings(self):
        from tests.test_actions import AUTH

        for status, code in ((403, "AWS_ACCESS_DENIED"), (503, "AWS_LOGIN_REQUIRED"),
                             (503, "AWS_CONNECTION_ERROR"), (503, "AWS_SCAN_INCOMPLETE")):
            self.scan.side_effect = AwsServiceError(status, code, "x")
            got_status, body = self.get(AUTH)
            self.assertEqual((got_status, body["detail"]["code"]), (status, code))
            self.assertNotIn("findings", body)

    def test_success_is_fresh_scan_each_request(self):
        from tests.test_actions import AUTH

        self.scan.side_effect = None
        self.scan.return_value = {"findings": [], "checked_groups": [], "checked_instance_count": 0,
                                  "checked_group_count": 0, "finding_count": 0, "checked_at": "2026-10-16T00:00:00Z"}
        for _ in range(2):
            status, body = self.get(AUTH)
            self.assertEqual(status, 200)
        self.assertEqual(self.scan.call_count, 2)
