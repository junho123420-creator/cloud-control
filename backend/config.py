"""앱 런타임 설정. 서버가 실행될 때만 프로젝트 루트의 .env를 읽는다."""

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT_DIR / ".env"
FRONTEND_DIR = ROOT_DIR / "frontend"

ALLOWED_REGION = "ap-northeast-2"
REQUIRED_VARS = ("AWS_PROFILE", "AWS_REGION", "EXPECTED_ACCOUNT_ID", "ADMIN_API_TOKEN")


class ConfigError(Exception):
    """설정 오류. 메시지에는 변수 이름만 담고 값은 담지 않는다."""


@dataclass(frozen=True)
class Settings:
    aws_profile: str
    aws_region: str
    expected_account_id: str
    admin_api_token: str
    allowed_instance_ids: frozenset[str]

    def __repr__(self) -> str:
        # 토큰이 로그·오류 메시지에 찍히지 않도록 값을 숨긴다.
        return (
            f"Settings(aws_profile=..., aws_region={self.aws_region!r}, "
            f"admin_api_token=***, allowed_instance_ids={len(self.allowed_instance_ids)}개)"
        )


def parse_id_list(raw: str) -> frozenset[str]:
    """쉼표로 나누고 앞뒤 공백과 빈 항목을 제거한 집합."""
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


DB_REQUIRED_VARS = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


@dataclass(frozen=True)
class DbSettings:
    host: str
    port: int
    name: str
    user: str
    password: str

    def __repr__(self) -> str:
        return f"DbSettings(host={self.host!r}, port={self.port}, name={self.name!r}, user={self.user!r}, password=***)"


@lru_cache
def get_db_settings() -> DbSettings:
    """DB 접속 설정. AWS·토큰 설정과 분리해 init_db에서도 단독으로 쓴다."""
    load_dotenv(ENV_FILE)

    values = {name: os.getenv(name, "").strip() for name in DB_REQUIRED_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(f"필수 설정이 없습니다: {', '.join(missing)} (.env.example 참고)")

    if not values["DB_PORT"].isdigit() or not 1 <= int(values["DB_PORT"]) <= 65535:
        raise ConfigError("DB_PORT는 1~65535 사이의 숫자여야 합니다.")

    return DbSettings(
        host=values["DB_HOST"],
        port=int(values["DB_PORT"]),
        name=values["DB_NAME"],
        user=values["DB_USER"],
        password=values["DB_PASSWORD"],
    )


@lru_cache
def get_settings() -> Settings:
    # 성공한 경우만 캐시된다. 실패하면 다음 호출에서 다시 읽는다.
    load_dotenv(ENV_FILE)

    values = {name: os.getenv(name, "").strip() for name in REQUIRED_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(f"필수 설정이 없습니다: {', '.join(missing)} (.env.example 참고)")

    if values["AWS_REGION"] != ALLOWED_REGION:
        raise ConfigError(f"AWS_REGION은 {ALLOWED_REGION}만 허용합니다.")

    if not re.fullmatch(r"\d{12}", values["EXPECTED_ACCOUNT_ID"]):
        raise ConfigError("EXPECTED_ACCOUNT_ID는 12자리 숫자여야 합니다.")

    return Settings(
        aws_profile=values["AWS_PROFILE"],
        aws_region=values["AWS_REGION"],
        expected_account_id=values["EXPECTED_ACCOUNT_ID"],
        admin_api_token=values["ADMIN_API_TOKEN"],
        # 비어 있어도 된다. 그 경우 조회만 허용하고 모든 제어를 거절한다.
        allowed_instance_ids=parse_id_list(os.getenv("ALLOWED_INSTANCE_IDS", "")),
    )
