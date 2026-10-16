"""FastAPI 진입점. 실행: uvicorn backend.main:app --host 127.0.0.1 --port 8001"""

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.aws_service import AwsService, AwsServiceError
from backend.config import FRONTEND_DIR, ConfigError, get_settings

app = FastAPI(title="Cloud Control")


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code, "message": message}})


@app.exception_handler(ConfigError)
def handle_config_error(request: Request, exc: ConfigError) -> JSONResponse:
    return error_response(500, "CONFIG_ERROR", str(exc))


@app.exception_handler(AwsServiceError)
def handle_aws_error(request: Request, exc: AwsServiceError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


def get_aws() -> AwsService:
    return AwsService(get_settings())


def verified_aws(aws: AwsService = Depends(get_aws)) -> AwsService:
    """모든 조회 전에 STS로 계정을 확인한다."""
    aws.get_context()
    return aws


# 동기 Boto3 호출이므로 일반 def로 선언해 스레드풀에서 실행되게 한다.
@app.get("/api/context")
def get_context(aws: AwsService = Depends(get_aws)):
    return aws.get_context()


@app.get("/api/ec2")
def list_ec2(aws: AwsService = Depends(verified_aws)):
    items = aws.list_ec2_instances()
    return {"items": items, "count": len(items)}


@app.get("/api/s3")
def list_s3(aws: AwsService = Depends(verified_aws)):
    items = aws.list_s3_buckets()
    return {"items": items, "count": len(items)}


@app.get("/api/security-groups")
def list_security_groups(aws: AwsService = Depends(verified_aws)):
    items = aws.list_security_groups()
    return {"items": items, "count": len(items)}


# API 라우트 뒤에 마운트해야 /api 경로가 가려지지 않는다. frontend 폴더만 공개한다.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
