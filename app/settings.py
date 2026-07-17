from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "SQL 预警系统"
    database_url: str = "sqlite:///./early_warning.sqlite3"
    scheduler_sync_interval_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    rule_execution_lease_seconds: int = Field(default=7200, gt=0)
    scheduler_misfire_grace_seconds: int = Field(default=300, gt=0)
    worker_heartbeat_timeout_seconds: int = Field(default=60, gt=0)
    log_retention_days: int = Field(default=180, gt=0)
    log_cleanup_interval_seconds: int = Field(default=86400, gt=0)
    session_max_age_seconds: int = Field(default=28800, gt=0)
    session_idle_timeout_seconds: int = Field(default=1800, gt=0)
    session_secret: str
    secret_key: str
    session_cookie_secure: bool = False
    login_max_failures: int = Field(default=5, gt=0)
    login_failure_window_seconds: int = Field(default=900, gt=0)
    login_lockout_seconds: int = Field(default=900, gt=0)

    @field_validator("session_secret")
    @classmethod
    def validate_session_secret(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        if value.startswith("REPLACE_ME"):
            raise ValueError(f"{info.field_name} must not use a REPLACE_ME placeholder")
        if len(value.encode("utf-8")) < 32:
            raise ValueError("session_secret must be at least 32 bytes")
        return value

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        if value.startswith("REPLACE_ME"):
            raise ValueError(f"{info.field_name} must not use a REPLACE_ME placeholder")
        try:
            Fernet(value.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("secret_key must be a valid Fernet key") from exc
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
