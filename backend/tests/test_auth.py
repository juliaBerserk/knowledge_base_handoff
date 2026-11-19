from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from estafeta.api.auth import COOKIE, ChangePasswordBody, RedeemBody, session_reply
from estafeta.auth import security, service
from estafeta.core.config import get_settings
from estafeta.main import app
from estafeta.models.auth import AuthSession, Credential

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_common_password_policy_and_argon2id() -> None:
    assert not security.password_is_allowed("short")
    assert not security.password_is_allowed("1234567891234567")
    password = "Пароль с церемонией и восемью грушами!"
    assert security.password_is_allowed(password)
    hashed = security.hash_password(password)
    assert hashed.startswith("$argon2id$")
    assert security.verify_password(password, hashed)
    assert not security.verify_password("Другой пароль", hashed)
    assert not security.verify_password(password, None)


def test_new_passwords_accept_mixed_characters_at_16_character_boundary() -> None:
    password = "Вз1dQm!аб9#Xёж7%"
    assert len(password) == 16
    assert not security.password_is_allowed(password[:-1])
    assert security.password_is_allowed(password)
    token = security.new_token()
    with pytest.raises(ValidationError):
        RedeemBody(token=token, password=password[:-1])
    with pytest.raises(ValidationError):
        ChangePasswordBody(current_password="старый пароль", new_password=password[:-1])
    assert RedeemBody(token=token, password=password).password == password
    assert ChangePasswordBody(
        current_password="старый пароль", new_password=password
    ).new_password == password


def test_totp_standard_vector_and_replay() -> None:
    secret = b"12345678901234567890"
    now = datetime.fromtimestamp(59, UTC)
    assert security.totp_code(secret, 1) == "287082"
    assert security.totp_step(secret, "287082", now) == 1
    assert security.totp_step(secret, "287082", now, last_step=1) is None
    assert security.totp_step(secret, "287082", now + timedelta(seconds=60)) is None
    cipher = security.totp_cipher(get_settings().app_secret_key)
    encrypted = cipher.encrypt(secret)
    assert secret not in encrypted
    assert cipher.decrypt(encrypted) == secret


def test_lockout_starts_on_fifth_failure_and_expires_in_15_minutes() -> None:
    credential = Credential(failures=0)
    for _ in range(4):
        service.record_failure(credential, NOW)
        assert not service.is_locked(credential, NOW)
    service.record_failure(credential, NOW)
    assert service.is_locked(credential, NOW + timedelta(minutes=14, seconds=59))
    assert not service.is_locked(credential, NOW + timedelta(minutes=15))
    assert credential.failures == 0


def test_session_checks_idle_and_absolute_limits() -> None:
    settings = get_settings()
    session = AuthSession(created_at=NOW, last_seen_at=NOW)
    assert not service.session_expired(session, settings, NOW + timedelta(minutes=29))
    assert service.session_expired(session, settings, NOW + timedelta(minutes=30))
    session.last_seen_at = NOW + timedelta(hours=11, minutes=59)
    assert not service.session_expired(session, settings, session.last_seen_at)
    assert service.session_expired(session, settings, NOW + timedelta(hours=12))


def test_cookie_is_secure_and_csrf_binds_to_session() -> None:
    settings = get_settings()
    token = security.new_token()
    response = session_reply(settings, token, False)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=43200"):
        assert attribute in cookie
    assert "Domain=" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert security.csrf_token(settings.app_secret_key, token) != security.csrf_token(
        settings.app_secret_key, security.new_token()
    )


@pytest.mark.parametrize("path", ["logout", "totp/start", "totp/confirm"])
def test_cookie_mutations_require_csrf(path: str) -> None:
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(COOKIE, security.new_token())
        response = client.post(f"/api/auth/{path}", json={"code": "123456"})
    assert response.status_code == 403


def test_cross_origin_posts_and_unsafe_form_encoding_are_rejected() -> None:
    with TestClient(app) as client:
        response = client.get("/api/auth/session", headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403
        response = client.post("/api/auth/login", json={}, headers={"Origin": "https://evil.test"})
        assert response.status_code == 403
        response = client.post("/api/auth/login", data={"login": "someone"})
        assert response.status_code == 415


def test_request_validation_never_echoes_password_or_token() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/redeem", json={"token": "example-secret", "password": "a"}
        )
    assert response.status_code == 422
    assert "example-secret" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_production_origin_requires_https_and_a_non_development_key() -> None:
    settings = get_settings()
    config = type(settings)
    with pytest.raises(ValidationError, match="требуется HTTPS"):
        config(database_url=settings.database_url, auth_public_origin="http://kb.example.test")
    with pytest.raises(ValidationError, match="уникальный APP_SECRET_KEY"):
        config(
            database_url=settings.database_url,
            auth_public_origin="https://kb.example.test",
            app_secret_key="dev-secret-key-change-me-32chars-min",
        )
