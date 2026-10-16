"""EC2 시작·중지 처리. 학습용 단일 프로세스 앱을 전제로 프로세스 전역 lock 하나로 직렬화한다.

순서: 허용 목록 → 예상 계정 → [lock] 중복 키 → 미확정 이력 → 최신 상태 → REQUESTED commit → AWS 호출 → 결과 기록
"""

import logging
import threading

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.aws_service import AwsService, classify_control_error
from backend.db import (
    create_requested_action,
    describe_db_error,
    find_pending_action,
    get_action_by_request_id,
    update_action_result,
)
from backend.errors import AppError
from backend.models import STATUS_ACCEPTED, ActionLog

logger = logging.getLogger("uvicorn.error")

# 프로세스에서 한 번만 만든다. 모든 변경 진입점(시작·중지)이 이 lock을 사용해야 한다.
CONTROL_LOCK = threading.Lock()

REQUIRED_STATE = {"start": "stopped", "stop": "running"}
ACTION_LABEL = {"start": "시작", "stop": "중지"}


def _db_unavailable() -> AppError:
    return AppError(503, "DB_UNAVAILABLE", "DB에 기록하지 못해 요청을 처리하지 않았습니다. AWS는 호출하지 않았습니다.")


def _rollback(session: Session | None) -> None:
    try:
        if session is not None:
            session.rollback()
    except Exception:
        pass


def _read(session: Session, func, *args):
    """DB 조회. 실패하면 503으로 바꾸며 AWS 변경 호출 전 단계에서만 쓴다."""
    try:
        return func(session, *args)
    except SQLAlchemyError as exc:
        logger.warning("DB 조회 실패: %s", describe_db_error(exc))
        _rollback(session)
        raise _db_unavailable() from None


def _summary(log: ActionLog) -> dict:
    return {"action_id": log.id, "request_id": log.request_id, "status": log.status}


def _replay(log: ActionLog, instance_id: str, action: str) -> tuple[int, dict]:
    """같은 request_id 재전송: 내용이 같으면 기존 이력을 그대로 돌려주고 다시 실행하지 않는다."""
    if log.instance_id != instance_id or log.action != action:
        raise AppError(409, "IDEMPOTENCY_CONFLICT", "같은 request_id로 다른 요청을 보낼 수 없습니다.")
    return 200, _summary(log)


def _record_result(session: Session, log: ActionLog, status: str, aws_request_id: str | None, message: str | None) -> bool:
    """AWS 호출 뒤 결과 기록. 실패해도 REQUESTED를 지우거나 다시 실행하지 않는다."""
    try:
        update_action_result(session, log, status, aws_request_id, message)
        return True
    except SQLAlchemyError as exc:
        logger.error("이력 갱신 실패 (action_id=%s, 의도한 상태=%s): %s", log.id, status, describe_db_error(exc))
        _rollback(session)
        return False


def request_action(session: Session, aws: AwsService, instance_id: str, action: str, request_id: str) -> tuple[int, dict]:
    if action not in REQUIRED_STATE:
        raise AppError(422, "VALIDATION_ERROR", "action은 start 또는 stop이어야 합니다.")

    # 1) 서버 허용 목록: AWS를 전혀 호출하지 않고 거절한다.
    aws.ensure_controllable(instance_id)
    # 2) 예상 계정 (STS 조회만 하며 변경 호출은 아니다)
    aws.get_context()

    with CONTROL_LOCK:
        # 3) 중복 키: 현재 EC2 상태 검사보다 먼저 판정한다.
        existing = _read(session, get_action_by_request_id, request_id)
        if existing is not None:
            return _replay(existing, instance_id, action)

        # 4) 같은 대상의 미확정(REQUESTED/UNKNOWN) 이력
        pending = _read(session, find_pending_action, instance_id)
        if pending is not None:
            raise AppError(
                409, "ACTION_IN_PROGRESS",
                f"이 인스턴스에 결과가 확정되지 않은 요청이 있습니다 (action_id={pending.id}, {pending.status}).",
            )

        # 5) 최신 EC2 상태
        state = aws.get_instance_state(instance_id)
        if state != REQUIRED_STATE[action]:
            raise AppError(
                409, "INVALID_STATE",
                f"현재 상태({state})에서는 {action}을 할 수 없습니다. {REQUIRED_STATE[action]} 상태에서만 가능합니다.",
            )

        # 6) REQUESTED commit. 실패하면 AWS를 호출하지 않는다.
        try:
            log = create_requested_action(session, request_id, instance_id, action)
        except IntegrityError:
            _rollback(session)
            existing = _read(session, get_action_by_request_id, request_id)
            if existing is not None:
                return _replay(existing, instance_id, action)
            raise _db_unavailable() from None
        except SQLAlchemyError as exc:
            logger.warning("REQUESTED 기록 실패: %s", describe_db_error(exc))
            _rollback(session)
            raise _db_unavailable() from None

        # 7) AWS 변경 호출 (한 번만, 재시도 없음)
        try:
            aws_request_id = aws.change_instance_state(instance_id, action)
        except Exception as exc:
            outcome, error = classify_control_error(exc)
            _record_result(session, log, outcome, None, f"{error.code}: {error.message}")
            raise error from None

        body = _summary(log) | {"status": STATUS_ACCEPTED}
        accepted_message = f"AWS가 {ACTION_LABEL[action]} 요청을 접수했습니다. 상태는 새로고침으로 확인하세요."
        if not _record_result(session, log, STATUS_ACCEPTED, aws_request_id, accepted_message):
            body["warning"] = "AWS는 요청을 접수했지만 이력 갱신에 실패했습니다. 이력은 REQUESTED로 남으며 다시 실행하지 마세요."
        return 202, body
