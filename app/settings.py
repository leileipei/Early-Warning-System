from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "SQL 预警系统"
    database_url: str = "sqlite:///./early_warning.sqlite3"
    session_secret: str
    secret_key: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
