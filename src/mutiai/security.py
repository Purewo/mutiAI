"""Password and opaque browser-session primitives."""

from __future__ import annotations

import hashlib
import secrets

from pwdlib import PasswordHash

_password_hash = PasswordHash.recommended()
_dummy_password_hash = _password_hash.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password while doing comparable work for unknown users."""

    return _password_hash.verify(
        password,
        password_hash if password_hash is not None else _dummy_password_hash,
    )


def create_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
