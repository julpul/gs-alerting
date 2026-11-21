import time
from uuid import uuid4

import jwt
import pytest

from app.config import get_settings
from app.core.security import (
    PAT_PREFIX,
    SOURCE_KEY_PREFIX,
    AuthError,
    create_token,
    decode_token,
    decrypt_secret,
    encrypt_secret,
    generate_api_key,
    generate_pat,
    hash_password,
    hash_token,
    verify_password,
)


def test_jwt_roundtrip():
    uid = uuid4()
    claims = decode_token(create_token(uid))
    assert claims["sub"] == str(uid)
    assert claims["typ"] == "access"
    assert claims["exp"] - claims["iat"] == get_settings().access_token_ttl_seconds


def test_jwt_expired_rejected():
    token = create_token(uuid4(), ttl_seconds=-5)
    with pytest.raises(AuthError):
        decode_token(token)


def test_jwt_wrong_type_rejected():
    with pytest.raises(AuthError):
        decode_token(create_token(uuid4(), "refresh"), "access")
    assert decode_token(create_token(uuid4(), "refresh"), "refresh")["typ"] == "refresh"


def test_jwt_tampered_or_foreign_secret_rejected():
    now = int(time.time())
    forged = jwt.encode({"sub": str(uuid4()), "typ": "access", "exp": now + 60}, "other-secret", "HS256")
    with pytest.raises(AuthError):
        decode_token(forged)
    with pytest.raises(AuthError):
        decode_token("not-a-jwt")


def test_password_hash_and_verify():
    h = hash_password("demo")
    assert h != "demo"
    assert verify_password("demo", h)
    assert not verify_password("wrong", h)
    assert not verify_password("demo", "garbage")


def test_api_key_and_pat_generation():
    key, prefix, digest = generate_api_key()
    assert key.startswith(SOURCE_KEY_PREFIX)
    assert key.startswith(prefix)
    assert digest == hash_token(key)
    assert len(digest) == 64
    pat, _, pat_digest = generate_pat()
    assert pat.startswith(PAT_PREFIX)
    assert pat_digest != digest
    assert generate_api_key()[0] != key


def test_secret_encryption_roundtrip():
    ciphertext = encrypt_secret("sk-live-123")
    assert b"sk-live" not in ciphertext
    assert decrypt_secret(ciphertext) == "sk-live-123"
