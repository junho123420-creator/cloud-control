"""없는 테이블만 생성한다. 기존 테이블과 데이터는 건드리지 않는다.

실행: python -m backend.init_db
"""

import sys

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from backend import models  # noqa: F401  모든 모델을 등록하기 위해 import
from backend.config import ConfigError, get_db_settings
from backend.db import describe_db_error, get_engine
from backend.models import Base


def main() -> int:
    try:
        s = get_db_settings()
    except ConfigError as exc:
        print(f"DB 설정 오류: {exc}", file=sys.stderr)
        return 1

    print(f"대상: {s.user}@{s.host}:{s.port}/{s.name}", flush=True)
    try:
        engine = get_engine()
        before = set(inspect(engine).get_table_names())
        Base.metadata.create_all(engine, checkfirst=True)
        after = set(inspect(engine).get_table_names())
    except SQLAlchemyError as exc:
        print(f"DB 초기화 실패: {describe_db_error(exc)}", file=sys.stderr)
        return 1

    for name in sorted(Base.metadata.tables):
        if name in before:
            print(f"  {name}: 이미 있음 (변경 없음)")
        elif name in after:
            print(f"  {name}: 생성함")
    return 0


if __name__ == "__main__":
    sys.exit(main())
