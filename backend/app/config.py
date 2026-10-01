from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    local_development: bool = False
    database_url: SecretStr
    redis_url: SecretStr
    s3_endpoint_url: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    s3_bucket: str = "recap-private"
    s3_region: str = "us-east-1"
    storage_backend: Literal["local", "s3", "minio"] = "local"
    local_storage_path: str = "./storage"
    processing_mode: Literal["local", "celery"] = "local"
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, ge=1_000_000)
    max_zip_entries: int = Field(default=500, ge=1, le=10_000)
    max_extracted_bytes: int = Field(default=500 * 1024 * 1024, ge=1_000_000)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    session_cookie_name: str = "recap_session"
    csrf_cookie_name: str = "recap_csrf"
    session_ttl_days: int = 30
    secure_cookies: bool = False
    auth_rate_attempts: int = Field(default=30, ge=1, le=1000)
    auth_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    allowed_origins: list[str] = ["http://localhost:3000", "http://localhost:8000"]
    llm_provider: Literal["openai", "gemini"] | None = None
    llm_model: str | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
