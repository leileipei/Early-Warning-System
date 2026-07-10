from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "SQL 预警系统"
    database_url: str = "sqlite:///./early_warning.sqlite3"
    scheduler_sync_interval_seconds: float = Field(default=10.0, gt=0)
    session_secret: str
    secret_key: str

    @field_validator("session_secret", "secret_key")
    @classmethod
    def validate_required_secret(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        if value.startswith("REPLACE_ME"):
            raise ValueError(f"{info.field_name} must not use a REPLACE_ME placeholder")
        if info.field_name == "secret_key":
            try:
                Fernet(value.encode("utf-8"))
            except (TypeError, ValueError) as exc:
                raise ValueError("secret_key must be a valid Fernet key") from exc
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
