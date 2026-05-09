"""Bcrypt helpers (direct bcrypt usage; passlib 1.7.4 is incompatible with
bcrypt >=4 due to a version-detection bug). Centralised so we can swap
algorithms later without touching every router.
"""
from __future__ import annotations

import bcrypt


def _clamp(plain: str) -> bytes:
    # Bcrypt only consumes the first 72 bytes of the password and bcrypt >=4
    # raises instead of silently truncating.
    return plain.encode("utf-8")[:72]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_clamp(plain), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_clamp(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
