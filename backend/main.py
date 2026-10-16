"""FastAPI 진입점. 실행: uvicorn backend.main:app --host 127.0.0.1 --port 8001"""

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.auth import require_admin_token
from backend.aws_service import AwsService, AwsServiceError
from backend.config import FRONTEND_DIR, ConfigError, get_db_settings, get_settings
from backend.db import describe_db_error, get_db, list_recent_actions

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ADMIN_API_TOKEN 등 필수 설정이 없으면 서버 시작을 거절한다. 메시지에는 변수 이름만 담긴다.
    try:
        get_settings()
        get_db_settings()
    except ConfigError as exc:
        logger.error("서버 시작 거절: %s", exc)
        raise SystemExit(1) from None
    yield


app = FastAPI(title="Cloud Control", lifespan=lifespan)


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code, "message": message}})


@app.exception_handler(ConfigError)
def handle_config_error(request: Request, exc: ConfigError) -> JSONResponse:
    return error_response(500, "CONFIG_ERROR", str(exc))


@app.exception_handler(AwsServiceError)
def handle_aws_error(request: Request, exc: AwsServiceError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(SQLAlchemyError)
def handle_db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    # 원인은 서버 로그에만 남긴다. 암호·URL은 포함되지 않는다.
    logger.warning("DB 오류: %s", describe_db_error(exc))
    if getattr(getattr(exc, "orig", None), "args", (None,))[0] == 1146:  # 테이블 없음
        return error_response(503, "DB_NOT_INITIALIZED", "DB 테이블이 없습니다. python -m backend.init_db를 실행하세요.")
    if isinstance(exc, OperationalError):
        return error_response(503, "DB_UNAVAILABLE", "DB에 연결하지 못했습니다. MariaDB 상태와 DB 설정을 확인하세요.")
    return error_response(500, "DB_ERROR", "DB 처리 중 오류가 발생했습니다.")


def get_aws() -> AwsService:
    return AwsService(get_settings())


def verified_aws(aws: AwsService = Depends(get_aws)) -> AwsService:
    """모든 조회 전에 STS로 계정을 확인한다."""
    aws.get_context()
    return aws


# 모든 /api 경로는 이 라우터에 등록해 관리자 토큰 인증을 AWS 호출보다 먼저 거친다.
# 계정·리전·허용 목록은 서버 설정에서만 읽으며 요청 파라미터로 받지 않는다.
api = APIRouter(prefix="/api", dependencies=[Depends(require_admin_token)])


# 동기 Boto3 호출이므로 일반 def로 선언해 스레드풀에서 실행되게 한다.
@api.get("/context")
def get_context(aws: AwsService = Depends(get_aws)):
    return aws.get_context()


@api.get("/ec2")
def list_ec2(aws: AwsService = Depends(verified_aws)):
    items = aws.list_ec2_instances()
    return {"items": items, "count": len(items)}


@api.get("/s3")
def list_s3(aws: AwsService = Depends(verified_aws)):
    items = aws.list_s3_buckets()
    return {"items": items, "count": len(items)}


@api.get("/security-groups")
def list_security_groups(aws: AwsService = Depends(verified_aws)):
    items = aws.list_security_groups()
    return {"items": items, "count": len(items)}


@api.get("/actions")
def list_actions(db: Session = Depends(get_db)):
    items = [log.to_dict() for log in list_recent_actions(db)]
    return {"items": items, "count": len(items)}


# 없는 /api 경로도 인증을 거친 뒤 JSON 404로 응답한다 (정적 파일로 넘어가지 않게).
@api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def api_not_found(path: str):
    return error_response(404, "NOT_FOUND", "없는 API 경로입니다.")


app.include_router(api)

# API 등록 뒤 마지막에 마운트한다. frontend 폴더만 공개하며 인증을 요구하지 않는다.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
