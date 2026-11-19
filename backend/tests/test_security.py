from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag

from estafeta.core.security import (
    PasswordPolicyError,
    decrypt_totp_secret,
    encrypt_totp_secret,
    hash_password,
    keyed_digest,
    new_token,
    token_digest,
    totp_code,
    totp_uri,
    validate_password,
    verify_password,
    verify_totp,
)


def _settings(monkeypatch, password_file: Path) -> None:
    monkeypatch.setattr(
        "estafeta.core.security.get_settings",
        lambda: SimpleNamespace(
            app_secret_key="test-secret-key-not-for-production-use",
            common_passwords_file=password_file,
        ),
    )


def test_token_generation_and_digests_are_one_way(monkeypatch, tmp_path) -> None:
    _settings(monkeypatch, tmp_path / "common.txt")
    first = new_token()
    second = new_token()

    assert first != second
    assert len(token_digest(first)) == 32
    assert token_digest(first) != first.encode()
    assert keyed_digest("value", "ip") != keyed_digest("value", "email")


def test_password_policy_and_argon2id(monkeypatch, tmp_path) -> None:
    common_passwords = tmp_path / "common.txt"
    common_passwords.write_text("Password123456\n", encoding="utf-8")
    _settings(monkeypatch, common_passwords)

    with pytest.raises(PasswordPolicyError, match="12 символов"):
        validate_password("short")
    with pytest.raises(PasswordPolicyError, match="частых"):
        validate_password("Password123456")

    encoded = hash_password("Correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    assert verify_password("Correct horse battery staple", encoded) is True
    assert verify_password("incorrect password", encoded) is False
    assert verify_password("any password", None) is False


def test_password_policy_fails_closed_for_missing_or_empty_list(monkeypatch, tmp_path) -> None:
    missing = tmp_path / "missing.txt"
    _settings(monkeypatch, missing)
    with pytest.raises(RuntimeError, match="недоступен"):
        validate_password("A unique long password")

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    _settings(monkeypatch, empty)
    with pytest.raises(RuntimeError, match="пуст"):
        validate_password("A unique long password")


def test_totp_encryption_roundtrip_and_authentication_tag(monkeypatch, tmp_path) -> None:
    _settings(monkeypatch, tmp_path / "common.txt")
    secret = bytes(range(20))
    nonce, ciphertext = encrypt_totp_secret(secret)

    assert decrypt_totp_secret(nonce, ciphertext) == secret
    with pytest.raises(InvalidTag):
        decrypt_totp_secret(nonce, ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))


def test_totp_matches_rfc6238_sha1_vector_and_time_window() -> None:
    secret = b"12345678901234567890"
    assert totp_code(secret, 59) == "287082"
    assert verify_totp(secret, "287082", 59) is True
    assert verify_totp(secret, "287082", 120) is False
    assert verify_totp(secret, "12345", 59) is False
    assert verify_totp(secret, "１２３４５６", 59) is False


def test_totp_uri_encodes_account_and_secret() -> None:
    uri = totp_uri(b"12345678901234567890", "user@example.test")

    assert uri.startswith("otpauth://totp/")
    assert "secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ" in uri
    assert "issuer=" in uri
