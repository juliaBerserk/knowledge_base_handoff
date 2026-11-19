import pytest
from pydantic import ValidationError

from estafeta.core.config import MigrationSettings, Settings, clear_settings_cache, get_settings


def test_settings_require_postgres_url() -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_secret_key="k" * 32,
            database_url="sqlite:///tmp.db",
        )


def test_cors_origin_list() -> None:
    settings = Settings(
        app_secret_key="k" * 32,
        database_url="postgresql+psycopg://app:x@localhost/db",
        cors_origins="http://localhost:5173, http://127.0.0.1:5173",
    )
    assert settings.cors_origin_list() == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_get_settings_from_env() -> None:
    clear_settings_cache()
    settings = get_settings()
    assert settings.app_name == "Эстафета"
    assert settings.session_idle_minutes == 30
    assert settings.session_max_hours == 12
    assert len(settings.app_secret_key) >= 32


def test_owner_url_is_only_used_by_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    owner_url = "postgresql+psycopg://owner:x@localhost/db"
    monkeypatch.setenv("DATABASE_OWNER_URL", owner_url)
    assert MigrationSettings().database_owner_url == owner_url
    assert "database_owner_url" not in Settings.model_fields
