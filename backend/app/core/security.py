"""Password hashing, JWT (HS256), API keys / personal access tokens, secrets at rest."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any, Literal
from uuid import UUID

import bcrypt
import jwt
from cryptography.fernet import Fernet

from app.config import get_settings

TokenType = Literal["access", "refresh"]
SOURCE_KEY_PREFIX = "ak_"
PAT_PREFIX = "pat_"


class AuthError(Exception):
    """Invalid, expired or wrongly typed token."""


def hash_password(password: str) -> str:
    rounds = get_settings().bcrypt_rounds
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_token(user_id: UUID, token_type: TokenType = "access", ttl_seconds: int | None = None) -> str:
    settings = get_settings()
    if ttl_seconds is None:
        ttl_seconds = (
            settings.access_token_ttl_seconds
            if token_type == "access"
            else settings.refresh_token_ttl_seconds
        )
    now = int(time.time())
    claims = {"sub": str(user_id), "typ": token_type, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str, expected_type: TokenType = "access") -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token, get_settings().jwt_secret, algorithms=["HS256"], options={"require": ["exp", "sub"]}
        )
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc
    if claims.get("typ") != expected_type:
        raise AuthError("wrong token type")
    return claims


def hash_token(token: str) -> str:
    """sha256 hex of an API key / PAT (they are high-entropy, so no salt/slow hash needed)."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_secret_token(prefix: str) -> tuple[str, str, str]:
    """Returns (plaintext shown once, display prefix, sha256 hash)."""
    token = prefix + secrets.token_urlsafe(32)
    return token, token[: len(prefix) + 6], hash_token(token)


def generate_api_key() -> tuple[str, str, str]:
    return generate_secret_token(SOURCE_KEY_PREFIX)


def generate_pat() -> tuple[str, str, str]:
    return generate_secret_token(PAT_PREFIX)


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().app_secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt_secret(ciphertext: bytes) -> str:
    return _fernet().decrypt(ciphertext).decode()
