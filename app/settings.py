from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "SQL 预警系统"
    database_url: str = "sqlite:///./early_warning.sqlite3"
    session_secret: str = Field(default="change-me-in-production")
    secret_key: str = Field(default="0123456789abcdef0123456789abcdef")


@lru_cache
def get_settings() -> Settings:
    return Settings()
