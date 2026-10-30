import os

os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-for-production-use")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://estafeta_app:estafeta_app@localhost:5432/estafeta",
)
os.environ.setdefault(
    "DATABASE_OWNER_URL",
    "postgresql+psycopg://estafeta_owner:estafeta_owner@localhost:5432/estafeta",
)
os.environ.setdefault("CORS_ORIGINS", "http://127.0.0.1:5173")

from estafeta.core.config import clear_settings_cache  # noqa: E402

clear_settings_cache()
