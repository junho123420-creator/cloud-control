"""EC2 제어 API 테스트. 실제 AWS나 실습 DB를 호출하지 않는다.

실행: python -m unittest -v tests.test_actions

- AWS: boto3.Session을 가짜로 바꿔 client 호출 횟수를 센다.
- DB: backend.actions가 쓰는 저장 함수를 메모리 저장소로 바꾼다.
- HTTP: 추가 패키지 없이 ASGI 앱을 직접 호출한다.
"""

import asyncio
import json
import os
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from botocore import exceptions as bex
from sqlalchemy.exc import IntegrityError, OperationalError

from backend import actions, config, db, main
from backend.models import ActionLog

ACCOUNT = "111111111111"
TOKEN = "test-admin-token"
ALLOWED = "i-0aaaaaaaaaaaaaaaa"
OTHER = "i-0bbbbbbbbbbbbbbbb"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

TEST_ENV = {
    "AWS_PROFILE": "test",
    "AWS_REGION": "ap-northeast-2",
    "EXPECTED_ACCOUNT_ID": ACCOUNT,
    "ADMIN_API_TOKEN": TOKEN,
    "ALLOWED_INSTANCE_IDS": ALLOWED,
}


def call(method: str, path: str, headers: dict | None = None, body=None) -> tuple[int, dict | None]:
    """ASGI 앱에 HTTP 요청 하나를 보낸다."""
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = [(b"host", b"testserver"), (b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    hdrs += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": hdrs, "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }
    messages, sent = [], False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    asyncio.run(main.app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, (json.loads(data) if data else None)


class FakeRepo:
    """backend.db 저장 함수의 메모리 대체본."""

    def __init__(self):
        self.logs: dict[str, ActionLog] = {}
        self.next_id = 1
        self.fail_create = None  # 예외를 넣으면 create에서 발생
        self.fail_update = None
        self.fail_read = None

    def get_action_by_request_id(self, session, request_id):
        if self.fail_read:
            raise self.fail_read
        return self.logs.get(request_id)

    def find_pending_action(self, session, instance_id):
        if self.fail_read:
            raise self.fail_read
        for log in self.logs.values():
            if log.instance_id == instance_id and log.status in ("REQUESTED", "UNKNOWN"):
                return log
        return None

    def create_requested_action(self, session, request_id, instance_id, action):
        if self.fail_create:
            raise self.fail_create
        if request_id in self.logs:
            raise IntegrityError("INSERT", {}, Exception("Duplicate entry"))
        log = ActionLog(id=self.next_id, request_id=request_id, instance_id=instance_id, action=action, status="REQUESTED")
        self.next_id += 1
        self.logs[request_id] = log
        return log

    def update_action_result(self, session, log, status, aws_request_id, message):
        if self.fail_update:
            raise self.fail_update
        log.status, log.aws_request_id, log.message = status, aws_request_id, message

    def add(self, request_id, instance_id, action, status):
        log = ActionLog(id=self.next_id, request_id=request_id, instance_id=instance_id, action=action, status=status)
        self.next_id += 1
        self.logs[request_id] = log
        return log


def db_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception(2003, "Can't connect"))


class ActionApiTest(unittest.TestCase):
    def setUp(self):
        # 실제 .env를 읽지 않도록 경로를 바꾸고 테스트용 환경 변수만 쓴다.
        patches = [
            mock.patch.object(config, "ENV_FILE", Path("/nonexistent/.env")),
            mock.patch.dict(os.environ, TEST_ENV),
        ]
        self.repo = FakeRepo()
        for name in ("get_action_by_request_id", "find_pending_action", "create_requested_action", "update_action_result"):
            patches.append(mock.patch.object(actions, name, getattr(self.repo, name)))

        # 가짜 AWS: 조회 client와 변경 client를 구분해 기록한다.
        self.state = "stopped"
        self.account = ACCOUNT
        self.ec2 = mock.MagicMock(name="ec2")
        self.ec2.describe_instances.side_effect = lambda **kw: {
            "Reservations": [{"Instances": [{"InstanceId": kw["InstanceIds"][0], "State": {"Name": self.state}}]}]
        }
        self.ec2.start_instances.return_value = {"ResponseMetadata": {"RequestId": "aws-req-start"}}
        self.ec2.stop_instances.return_value = {"ResponseMetadata": {"RequestId": "aws-req-stop"}}
        self.sts = mock.MagicMock(name="sts")
        self.sts.get_caller_identity.side_effect = lambda: {"Account": self.account, "Arn": "arn:aws:iam::x:user/t"}
        self.client_configs = []

        def client(name, config=None):
            self.client_configs.append((name, config))
            return self.sts if name == "sts" else self.ec2

        fake_session = mock.MagicMock()
        fake_session.client.side_effect = client
        patches.append(mock.patch("backend.aws_service.boto3.Session", return_value=fake_session))

        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        config.get_settings.cache_clear()
        self.addCleanup(config.get_settings.cache_clear)

        def fake_get_db():
            yield None  # 세션은 쓰지 않는다 (저장 함수를 대체했으므로)

        main.app.dependency_overrides[db.get_db] = fake_get_db
        self.addCleanup(main.app.dependency_overrides.clear)

    # ---- 도우미 ----

    def post(self, instance_id=ALLOWED, action="start", request_id=None, headers=AUTH, body=None):
        if body is None:
            body = {"action": action, "request_id": request_id or str(uuid.uuid4())}
        return call("POST", f"/api/ec2/{instance_id}/actions", headers, body)

    def change_calls(self) -> int:
        return self.ec2.start_instances.call_count + self.ec2.stop_instances.call_count

    # ---- 인증·입력 ----

    def test_invalid_token_makes_no_change_call(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}, {"Authorization": "Bearer "}):
            status, body = self.post(headers=headers)
            self.assertEqual(status, 401)
            self.assertEqual(body["detail"]["code"], "APP_AUTH_REQUIRED")
        self.assertEqual(self.change_calls(), 0)
        self.assertEqual(self.client_configs, [])  # AWS client 자체를 만들지 않음
        self.assertEqual(self.repo.logs, {})

    def test_extra_field_and_bad_uuid_are_422(self):
        rid = str(uuid.uuid4())
        bad_bodies = [
            {"action": "start", "request_id": rid, "region": "us-east-1"},
            {"action": "start", "request_id": "not-a-uuid"},
            {"action": "start", "request_id": str(uuid.uuid1())},  # v4가 아님
            {"action": "terminate", "request_id": rid},
            {"action": "start"},
        ]
        for body in bad_bodies:
            status, resp = self.post(body=body)
            self.assertEqual(status, 422, body)
            self.assertEqual(resp["detail"]["code"], "VALIDATION_ERROR")
        self.assertEqual(self.change_calls(), 0)

    # ---- 허용 목록·계정 ----

    def test_not_allowed_instance_is_403_before_any_aws_call(self):
        status, body = self.post(instance_id=OTHER)
        self.assertEqual(status, 403)
        self.assertEqual(body["detail"]["code"], "INSTANCE_NOT_ALLOWED")
        self.assertEqual(self.change_calls(), 0)
        self.ec2.describe_instances.assert_not_called()
        self.sts.get_caller_identity.assert_not_called()
        self.assertEqual(self.repo.logs, {})

    def test_empty_allowlist_rejects_all_control(self):
        with mock.patch.dict(os.environ, {"ALLOWED_INSTANCE_IDS": " , "}):
            config.get_settings.cache_clear()
            status, body = self.post()
        self.assertEqual(status, 403)
        self.assertEqual(body["detail"]["code"], "CONTROL_DISABLED")
        self.assertEqual(self.change_calls(), 0)

    def test_account_mismatch_is_403(self):
        self.account = "222222222222"
        status, body = self.post()
        self.assertEqual(status, 403)
        self.assertEqual(body["detail"]["code"], "AWS_ACCOUNT_MISMATCH")
        self.assertEqual(self.change_calls(), 0)

    # ---- DB 실패 ----

    def test_db_failure_on_create_is_503_and_no_change_call(self):
        self.repo.fail_create = db_error()
        status, body = self.post()
        self.assertEqual(status, 503)
        self.assertEqual(body["detail"]["code"], "DB_UNAVAILABLE")
        self.assertEqual(self.change_calls(), 0)

    def test_db_failure_on_read_is_503_and_no_change_call(self):
        self.repo.fail_read = db_error()
        status, body = self.post()
        self.assertEqual(status, 503)
        self.assertEqual(self.change_calls(), 0)

    # ---- 정상 접수·중복 키 ----

    def test_accept_then_same_key_resend_calls_aws_once(self):
        rid = str(uuid.uuid4())
        status, first = self.post(request_id=rid)
        self.assertEqual(status, 202)
        self.assertEqual(first["status"], "ACCEPTED")
        self.assertEqual(first["request_id"], rid)

        for _ in range(3):
            status, again = self.post(request_id=rid)
            self.assertEqual(status, 200)
            self.assertEqual(again, {"action_id": first["action_id"], "request_id": rid, "status": "ACCEPTED"})

        self.assertEqual(self.change_calls(), 1)
        self.assertEqual(self.repo.logs[rid].aws_request_id, "aws-req-start")
        self.assertIn("접수", self.repo.logs[rid].message)

    def test_same_key_resend_is_checked_before_state(self):
        rid = str(uuid.uuid4())
        self.assertEqual(self.post(request_id=rid)[0], 202)
        self.state = "pending"  # 상태가 바뀌어도 재전송은 기존 결과를 돌려준다
        self.assertEqual(self.post(request_id=rid)[0], 200)
        self.assertEqual(self.change_calls(), 1)

    def test_same_key_different_content_is_409(self):
        rid = str(uuid.uuid4())
        self.assertEqual(self.post(request_id=rid)[0], 202)
        status, body = self.post(request_id=rid, action="stop")
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.change_calls(), 1)

    def test_unique_conflict_on_insert_returns_existing(self):
        rid = str(uuid.uuid4())
        existing = self.repo.add(rid, ALLOWED, "start", "ACCEPTED")
        # 조회 시점에는 없었다가 insert 시점에 생긴 경우를 흉내 낸다.
        lookups = iter([None, existing])
        with mock.patch.object(actions, "get_action_by_request_id", lambda s, r: next(lookups)):
            status, body = self.post(request_id=rid)
        self.assertEqual(status, 200)
        self.assertEqual(body["action_id"], existing.id)
        self.assertEqual(self.change_calls(), 0)

    # ---- 상태·미확정 이력 ----

    def test_state_mismatch_is_409(self):
        for state, action in (("running", "start"), ("stopped", "stop"), ("pending", "start"), ("stopping", "stop")):
            self.state = state
            status, body = self.post(action=action)
            self.assertEqual(status, 409, (state, action))
            self.assertEqual(body["detail"]["code"], "INVALID_STATE")
        self.assertEqual(self.change_calls(), 0)
        self.assertEqual(self.repo.logs, {})

    def test_stop_from_running_is_accepted(self):
        self.state = "running"
        status, body = self.post(action="stop")
        self.assertEqual(status, 202)
        self.ec2.stop_instances.assert_called_once_with(InstanceIds=[ALLOWED])  # Force 없음

    def test_existing_requested_or_unknown_blocks_new_request(self):
        for pending_status in ("REQUESTED", "UNKNOWN"):
            self.repo = FakeRepo()
            with mock.patch.multiple(
                actions,
                get_action_by_request_id=self.repo.get_action_by_request_id,
                find_pending_action=self.repo.find_pending_action,
                create_requested_action=self.repo.create_requested_action,
            ):
                self.repo.add(str(uuid.uuid4()), ALLOWED, "start", pending_status)
                status, body = self.post()
            self.assertEqual(status, 409, pending_status)
            self.assertEqual(body["detail"]["code"], "ACTION_IN_PROGRESS")
            self.assertEqual(len(self.repo.logs), 1)
        self.assertEqual(self.change_calls(), 0)

    # ---- AWS 결과 ----

    def test_timeout_records_unknown_and_never_calls_again(self):
        self.ec2.start_instances.side_effect = bex.ReadTimeoutError(endpoint_url="https://ec2")
        rid = str(uuid.uuid4())
        status, body = self.post(request_id=rid)
        self.assertEqual(status, 503)
        self.assertEqual(body["detail"]["code"], "AWS_RESULT_UNKNOWN")
        self.assertEqual(self.repo.logs[rid].status, "UNKNOWN")
        self.assertEqual(self.change_calls(), 1)

        # 같은 키 재전송: 기존 UNKNOWN을 돌려주고 다시 호출하지 않는다.
        status, again = self.post(request_id=rid)
        self.assertEqual((status, again["status"]), (200, "UNKNOWN"))
        # 새 키로 요청해도 UNKNOWN이 남아 있어 차단된다.
        status, blocked = self.post()
        self.assertEqual((status, blocked["detail"]["code"]), (409, "ACTION_IN_PROGRESS"))
        self.assertEqual(self.change_calls(), 1)

    def test_explicit_aws_rejection_records_failed(self):
        self.ec2.start_instances.side_effect = bex.ClientError(
            {"Error": {"Code": "UnauthorizedOperation"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "StartInstances"
        )
        rid = str(uuid.uuid4())
        status, body = self.post(request_id=rid)
        self.assertEqual(status, 403)
        self.assertEqual(self.repo.logs[rid].status, "FAILED")
        # FAILED는 미확정이 아니므로 새 요청을 막지 않는다.
        self.ec2.start_instances.side_effect = None
        self.assertEqual(self.post()[0], 202)
        self.assertEqual(self.change_calls(), 2)

    def test_aws_5xx_records_unknown(self):
        self.ec2.start_instances.side_effect = bex.ClientError(
            {"Error": {"Code": "InternalError"}, "ResponseMetadata": {"HTTPStatusCode": 500}}, "StartInstances"
        )
        rid = str(uuid.uuid4())
        self.assertEqual(self.post(request_id=rid)[0], 503)
        self.assertEqual(self.repo.logs[rid].status, "UNKNOWN")

    def test_db_update_failure_after_accept_keeps_requested(self):
        self.repo.fail_update = db_error()
        rid = str(uuid.uuid4())
        status, body = self.post(request_id=rid)
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "ACCEPTED")
        self.assertIn("warning", body)
        self.assertIn(rid, self.repo.logs)  # 삭제하지 않음
        self.assertEqual(self.repo.logs[rid].status, "REQUESTED")
        self.assertEqual(self.change_calls(), 1)

    # ---- 순서·lock·client 설정 ----

    def test_requested_is_committed_before_aws_call_and_lock_is_held(self):
        seen = {}

        def start(**kw):
            seen["logs"] = {r: log.status for r, log in self.repo.logs.items()}
            seen["locked"] = actions.CONTROL_LOCK.locked()
            return {"ResponseMetadata": {"RequestId": "x"}}

        self.ec2.start_instances.side_effect = start
        rid = str(uuid.uuid4())
        self.assertEqual(self.post(request_id=rid)[0], 202)
        self.assertEqual(seen["logs"], {rid: "REQUESTED"})
        self.assertTrue(seen["locked"])
        self.assertFalse(actions.CONTROL_LOCK.locked())

    def test_lock_is_single_process_wide_instance(self):
        lock = actions.CONTROL_LOCK
        self.assertIsInstance(lock, type(threading.Lock()))
        self.post()
        self.post()
        self.assertIs(actions.CONTROL_LOCK, lock)

    def test_change_client_uses_single_attempt_and_timeouts(self):
        self.assertEqual(self.post()[0], 202)
        configs = [cfg for name, cfg in self.client_configs if name == "ec2" and cfg.retries.get("total_max_attempts") == 1]
        self.assertEqual(len(configs), 1)
        self.assertEqual((configs[0].connect_timeout, configs[0].read_timeout), (3, 10))


if __name__ == "__main__":
    unittest.main()
