"""
Minimal auth for Sahayam.

Scope note: this is a prototype-grade auth layer -- password hashing is real
(PBKDF2-HMAC-SHA256, salted), but sessions are an in-memory token store (like
the in-memory priority queue elsewhere in this backend), so logins reset if
the server restarts. That's an intentional simplification for a college
mini-project demo; a production version would move sessions into the
database or a proper store (Redis, signed JWTs, etc).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
_ITERATIONS = 100_000


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS).hex()
    return hmac.compare_digest(check, digest)


# ---------------------------------------------------------------------------
# In-memory session tokens: token -> user_id
# ---------------------------------------------------------------------------
_SESSIONS: dict[str, int] = {}


def create_session(user_id: int) -> str:
    token = secrets.token_hex(24)
    _SESSIONS[token] = user_id
    return token


def get_user_id_for_token(token: str) -> int | None:
    return _SESSIONS.get(token)


def destroy_session(token: str) -> None:
    _SESSIONS.pop(token, None)
