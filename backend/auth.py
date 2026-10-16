"""모든 /api 경로에 적용하는 관리자 토큰 인증."""

import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config import get_settings

# auto_error=False: 헤더 누락·다른 scheme·빈 값이면 예외 대신 None을 받아 직접 처리한다.
bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "APP_AUTH_REQUIRED", "message": "관리자 토큰이 없거나 올바르지 않습니다."},
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_admin_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    supplied = credentials.credentials.strip()
    if not supplied:
        raise _unauthorized()

    expected = get_settings().admin_api_token
    # str끼리 비교하면 비ASCII 입력에서 TypeError(500)가 나므로 bytes로 비교한다.
    if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise _unauthorized()
