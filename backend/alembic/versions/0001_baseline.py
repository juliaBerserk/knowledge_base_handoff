"""Базовая ревизия: расширение vector создаётся init-скриптом PostgreSQL.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-30
"""

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Таблицы предметной области появятся на следующих этапах.
    # Миграции применяет роль estafeta_owner, не суперпользователь.
    return


def downgrade() -> None:
    return
