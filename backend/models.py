"""DB 모델. 시각은 모두 UTC 기준의 timezone 없는 DATETIME으로 저장한다."""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Index, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TABLE_ARGS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}

# 이력 상태: AWS 호출 전 REQUESTED로 기록하고 결과에 따라 갱신한다.
STATUS_REQUESTED = "REQUESTED"
STATUS_ACCEPTED = "ACCEPTED"  # AWS가 접수함
STATUS_FAILED = "FAILED"  # AWS가 명시적으로 거절했거나 전송되지 않음
STATUS_UNKNOWN = "UNKNOWN"  # 결과 불확실 (콘솔 확인 필요)
PENDING_STATUSES = (STATUS_REQUESTED, STATUS_UNKNOWN)  # 같은 대상의 새 제어를 막는 상태
MESSAGE_MAX = 500


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class ActionLog(Base):
    """EC2 시작·중지 요청 이력. AWS 호출 전에 기록하고 결과로 status를 갱신한다."""

    __tablename__ = "action_logs"
    __table_args__ = (Index("ix_action_logs_created_at", "created_at"), TABLE_ARGS)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)  # UUID
    instance_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # start | stop
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # STATUS_* 참고
    aws_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    message: Mapped[str | None] = mapped_column(String(MESSAGE_MAX), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "instance_id": self.instance_id,
            "action": self.action,
            "status": self.status,
            "aws_request_id": self.aws_request_id,
            # DB에는 timezone 없이 UTC로 저장되어 있으므로 응답에 Z를 붙인다.
            "created_at": self.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "message": self.message,
        }
