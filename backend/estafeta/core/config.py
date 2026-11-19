from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки из окружения. Секреты в репозиторий не попадают."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "Эстафета"
    app_secret_key: str = Field(min_length=32)
    database_url: str
    database_admin_url: str = ""
    cors_origins: str = ""

    llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_model: str = "local"
    llm_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"

    session_idle_minutes: int = Field(default=30, gt=0)
    session_max_hours: int = Field(default=12, gt=0)
    bootstrap_admin_email: str = ""
    auth_public_origin: str = "http://localhost:8080"
    auth_trust_proxy_ip: bool = False
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    tz: str = "Europe/Moscow"

    @field_validator("database_url")
    @classmethod
    def require_postgres(cls, value: str) -> str:
        if not value.startswith("postgresql"):
            raise ValueError("нужен PostgreSQL URL (postgresql+psycopg://...)")
        return value

    def cors_origin_list(self) -> list[str]:
        if not self.cors_origins.strip():
            return []
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @field_validator("auth_public_origin")
    @classmethod
    def require_origin(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            not url.hostname
            or url.username is not None
            or url.path
            or url.query
            or url.fragment
            or value != f"{url.scheme}://{url.netloc}"
            or url.port == (443 if url.scheme == "https" else 80)
        ):
            raise ValueError("Укажите origin без пути и параметров")
        if url.scheme == "https":
            return value
        if url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}:
            return value
        raise ValueError("Публичному адресу аутентификации требуется HTTPS")

    @model_validator(mode="after")
    def require_deployment_secret(self) -> "Settings":
        if (
            self.auth_public_origin.startswith("https://")
            and self.app_secret_key == "dev-secret-key-change-me-32chars-min"
        ):
            raise ValueError("Для HTTPS задайте уникальный APP_SECRET_KEY")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()


class MigrationSettings(BaseSettings):
    model_config = Settings.model_config

    database_owner_url: str
