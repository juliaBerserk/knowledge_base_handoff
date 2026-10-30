from functools import lru_cache

from pydantic import Field, field_validator
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

    session_idle_minutes: int = 30
    session_max_hours: int = 12
    bootstrap_admin_email: str = ""
    tz: str = "Europe/Moscow"

    @field_validator("database_url", "database_owner_url")
    @classmethod
    def require_postgres(cls, value: str) -> str:
        if not value.startswith("postgresql"):
            raise ValueError("нужен PostgreSQL URL (postgresql+psycopg://...)")
        return value

    def cors_origin_list(self) -> list[str]:
        if not self.cors_origins.strip():
            return []
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
