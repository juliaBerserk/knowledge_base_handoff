from collections.abc import Generator
from time import sleep
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from estafeta.core.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
    return _session_factory


def reset_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def set_local_user_id(session: Session, user_id: UUID) -> None:
    """Устанавливает контекст RLS локально для текущей транзакции."""
    session.execute(
        text("SELECT set_config('app.user_id', :user_id, true)"),
        {"user_id": str(user_id)},
    )


def set_local_context(session: Session, name: str, value: str) -> None:
    allowed = {
        "app.invitation_hash",
        "app.login_email",
        "app.rate_limit_hash",
        "app.session_hash",
        "app.session_idle_minutes",
    }
    if name not in allowed:
        raise ValueError(f"Недопустимый параметр контекста БД: {name}")
    session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": name, "value": value},
    )


def session_scope(user_id: UUID | None = None) -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        if user_id is not None:
            set_local_user_id(session, user_id)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping(engine: Engine) -> tuple[bool, bool]:
    """Проверка соединения и расширения pgvector. Возвращает (database, vector)."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        vector_ok = bool(
            connection.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            ).scalar()
        )
    return True, vector_ok


def wait_for_db(engine: Engine, attempts: int = 30, delay_seconds: float = 1.0) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            ping(engine)
            return
        except OperationalError as exc:
            last_error = exc
            sleep(delay_seconds)
    raise RuntimeError("База данных недоступна") from last_error
