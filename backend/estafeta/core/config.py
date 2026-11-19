from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки из окружения. Секреты в репозиторий не попадают."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Эстафета"
    app_secret_key: str = Field(min_length=32)
    database_url: str
    database_owner_url: str
    cors_origins: str = ""

    llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_model: str = "local"
    llm_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"
    build_job_poll_seconds: float = Field(default=2.0, gt=0, le=60)

    session_idle_minutes: int = Field(default=30, gt=0)
    session_max_hours: int = Field(default=12, gt=0)
    auth_cookie_secure: bool = True
    common_passwords_file: Path = Path("/run/secrets/common-passwords.txt")
    bootstrap_admin_email: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    public_base_url: str = "http://127.0.0.1:8080"
    tz: str = "Europe/Moscow"

    @field_validator("database_url", "database_owner_url")
    @classmethod
    def require_postgres(cls, value: str) -> str:
        if not value.startswith("postgresql"):
            raise ValueError("нужен PostgreSQL URL (postgresql+psycopg://...)")
        return value

    @model_validator(mode="after")
    def validate_mail_settings(self) -> "Settings":
        if self.smtp_host and not self.smtp_from:
            raise ValueError("SMTP_FROM обязателен при настроенном SMTP_HOST")
        if bool(self.smtp_user) != bool(self.smtp_password):
            raise ValueError("SMTP_USER и SMTP_PASSWORD должны быть заданы вместе")
        return self

    def cors_origin_list(self) -> list[str]:
        if not self.cors_origins.strip():
            return []
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
