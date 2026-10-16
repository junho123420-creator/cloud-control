"""DB 연결과 세션. FastAPI(main.py)에 의존하지 않고 config와 모델만 사용한다."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from backend.config import get_db_settings
from backend.models import ActionLog

RECENT_ACTIONS_LIMIT = 50


def build_url() -> URL:
    s = get_db_settings()
    return URL.create(
        drivername="mysql+pymysql",
        username=s.user,
        password=s.password,
        host=s.host,
        port=s.port,
        database=s.name,
        query={"charset": "utf8mb4"},
    )


@lru_cache
def get_engine() -> Engine:
    # 엔진(커넥션 풀)은 프로세스에 하나, 세션은 요청마다 새로 연다.
    return create_engine(
        build_url(),
        pool_pre_ping=True,
        pool_recycle=3600,
        hide_parameters=True,  # 오류 메시지에 SQL 파라미터 값을 넣지 않는다.
        connect_args={"connect_timeout": 5, "read_timeout": 10, "write_timeout": 10},
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """요청별 세션. 요청이 끝나면 반드시 닫는다."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def list_recent_actions(session: Session, limit: int = RECENT_ACTIONS_LIMIT) -> list[ActionLog]:
    stmt = select(ActionLog).order_by(ActionLog.created_at.desc(), ActionLog.id.desc()).limit(limit)
    return list(session.scalars(stmt))


def describe_db_error(exc: Exception) -> str:
    """비밀값 없이 원인만 설명한다. PyMySQL 오류는 (코드, 메시지) 형태이며 암호를 담지 않는다."""
    orig = getattr(exc, "orig", None)
    if orig is not None and len(getattr(orig, "args", ())) >= 2:
        code, msg = orig.args[0], orig.args[1]
        return f"{type(orig).__name__} {code}: {msg}"
    return type(orig or exc).__name__
