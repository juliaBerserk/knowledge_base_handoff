from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from estafeta.api.dependencies import (
    Principal,
    clear_session_cookies,
    get_db_session,
    request_client_ip,
    require_full_auth,
    set_session_cookies,
    verify_csrf,
)
from estafeta.core.security import hash_password, token_digest
from estafeta.domain.access import Role
from estafeta.main import app
from estafeta.models import AuditEvent, AuthSession, Base, Credential, User


def _request(
    *,
    cookies: dict[str, str] | None = None,
    headers: dict[bytes, bytes] | None = None,
) -> Request:
    request_headers = dict(headers or {})
    if cookies:
        cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
        request_headers[b"cookie"] = cookie_header.encode()
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/logout",
            "headers": list(request_headers.items()),
            "query_string": b"",
            "server": ("test", 80),
            "client": ("192.0.2.1", 1234),
            "scheme": "http",
        }
    )


def _principal(
    *,
    role: Role = Role.EMPLOYEE,
    verified: bool = True,
    totp_enabled: bool = False,
) -> Principal:
    user = User(
        id=uuid4(),
        full_name="Тестовый пользователь",
        email="user@example.test",
        role=role,
        is_active=True,
    )
    auth_session = AuthSession(
        id=uuid4(),
        user_id=user.id,
        token_hash=token_digest("session-token"),
        csrf_hash=token_digest("csrf-token"),
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=12),
        totp_verified_at=datetime.now(UTC) if verified else None,
    )
    return Principal(user, auth_session, totp_enabled=totp_enabled)


def test_csrf_requires_matching_cookie_header_and_session_digest() -> None:
    principal = _principal()
    request = _request(
        cookies={"estafeta_csrf": "csrf-token"},
        headers={b"x-csrf-token": b"csrf-token"},
    )

    verify_csrf(request, principal)

    with pytest.raises(HTTPException) as exc_info:
        verify_csrf(_request(cookies={"estafeta_csrf": "other"}), principal)
    assert exc_info.value.status_code == 403


def test_csrf_rejects_mismatched_header_and_cookie() -> None:
    principal = _principal()
    request = _request(
        cookies={"estafeta_csrf": "csrf-token"},
        headers={b"x-csrf-token": b"other"},
    )
    with pytest.raises(HTTPException) as exc_info:
        verify_csrf(request, principal)
    assert exc_info.value.status_code == 403


def test_cookie_attributes_are_secure_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        "estafeta.api.dependencies.get_settings",
        lambda: SimpleNamespace(auth_cookie_secure=True),
    )
    response = Response()

    set_session_cookies(
        response,
        "session-value",
        "csrf-value",
        datetime.now(UTC) + timedelta(hours=1),
    )
    cookies = response.headers.getlist("set-cookie")

    assert any(
        "estafeta_session=session-value" in item and "httponly" in item.lower() for item in cookies
    )
    assert any(
        "estafeta_csrf=csrf-value" in item and "httponly" not in item.lower() for item in cookies
    )
    assert all("secure" in item.lower() and "samesite=lax" in item.lower() for item in cookies)

    clear_session_cookies(response)
    assert len(response.headers.getlist("set-cookie")) == 4


def test_full_auth_requires_totp_for_admins_and_enabled_users() -> None:
    ordinary = _principal()
    assert require_full_auth(ordinary) is ordinary

    admin = _principal(role=Role.ADMIN, verified=False)
    with pytest.raises(Exception) as exc_info:
        require_full_auth(admin)
    assert getattr(exc_info.value, "status_code", None) == 403

    enabled_totp = _principal(verified=False, totp_enabled=True)
    with pytest.raises(HTTPException) as exc_info:
        require_full_auth(enabled_totp)
    assert exc_info.value.status_code == 403


def test_rate_limit_uses_validated_proxy_ip_and_falls_back_to_client() -> None:
    request = _request(headers={b"x-real-ip": b"198.51.100.8"})
    assert request_client_ip(request) == "198.51.100.8"

    spoofed = _request(headers={b"x-real-ip": b"not-an-ip"})
    assert request_client_ip(spoofed) == "192.0.2.1"


def test_protected_auth_endpoints_require_login_without_opening_database() -> None:
    client = TestClient(app)
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "Требуется вход"

    response = client.post("/api/admin/users", json={})
    assert response.status_code == 401


def test_login_persists_lockout_and_issues_cookie_after_valid_password(
    monkeypatch,
    tmp_path,
) -> None:
    password_file = tmp_path / "common-passwords.txt"
    password_file.write_text("Password123456\n", encoding="utf-8")
    settings = SimpleNamespace(
        app_secret_key="test-secret-key-not-for-production-use",
        common_passwords_file=password_file,
        session_max_hours=12,
        session_idle_minutes=30,
        auth_cookie_secure=False,
    )
    monkeypatch.setattr("estafeta.core.security.get_settings", lambda: settings)
    monkeypatch.setattr("estafeta.services.auth.get_settings", lambda: settings)
    monkeypatch.setattr("estafeta.api.dependencies.get_settings", lambda: settings)
    monkeypatch.setattr("estafeta.services.auth.set_local_context", lambda *_args: None)
    monkeypatch.setattr("estafeta.services.auth.set_local_user_id", lambda *_args: None)
    monkeypatch.setattr(
        "estafeta.services.auth_provider.set_local_context",
        lambda *_args: None,
    )
    monkeypatch.setattr("estafeta.services.auth._check_ip_limit", lambda *_args: None)
    monkeypatch.setattr("estafeta.services.auth._record_ip_failure", lambda *_args: None)
    monkeypatch.setattr("estafeta.services.auth._clear_ip_failures", lambda *_args: None)

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'auth-api.sqlite'}")
    Base.metadata.create_all(engine)
    user_id = uuid4()
    with Session(engine) as session:
        user = User(
            id=user_id,
            full_name="API test user",
            email="api.user@example.com",
            role=Role.EMPLOYEE,
            is_active=True,
        )
        session.add(user)
        session.flush()
        session.add(
            Credential(
                user_id=user_id,
                password_hash=hash_password("Correct horse battery staple"),
            )
        )
        session.commit()

    def override_session():
        session = Session(engine)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db_session] = override_session
    try:
        client = TestClient(app)
        payload = {
            "email": "api.user@example.com",
            "password": "Wrong password candidate",
        }
        for _ in range(5):
            assert client.post("/api/auth/login", json=payload).status_code == 401
        with Session(engine) as session:
            credential = session.get(Credential, user_id)
            assert credential is not None
            assert credential.failed_attempts == 5
            assert credential.locked_at is not None

        payload["password"] = "Correct horse battery staple"
        assert client.post("/api/auth/login", json=payload).status_code == 401
        with Session(engine) as session:
            credential = session.get(Credential, user_id)
            assert credential is not None
            credential.failed_attempts = 0
            credential.locked_at = None
            session.commit()

        response = client.post("/api/auth/login", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["authenticated"] is True
        assert body["csrf_token"]
        session_cookie = response.cookies.get("estafeta_session")
        csrf_cookie = response.cookies.get("estafeta_csrf")
        assert session_cookie is not None
        assert csrf_cookie == body["csrf_token"]
        cookie_headers = response.headers.get_list("set-cookie")
        assert any(
            "estafeta_session=" in header and "httponly" in header.lower()
            for header in cookie_headers
        )
        assert any(
            "estafeta_csrf=" in header and "httponly" not in header.lower()
            for header in cookie_headers
        )
        with Session(engine) as session:
            auth_session = session.scalar(select(AuthSession).where(AuthSession.user_id == user_id))
            assert auth_session is not None
            assert auth_session.token_hash == token_digest(session_cookie)
            audit_events = list(session.scalars(select(AuditEvent)))
            event_types = [event.event_type for event in audit_events]
            assert event_types.count("auth.login_failed") == 6
            assert event_types.count("auth.login_succeeded") == 1
            assert all(event.metadata_json == {} for event in audit_events)
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        engine.dispose()
