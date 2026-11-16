"""Password hashing (Argon2id), client address and the panel session."""

import base64
import hashlib
import hmac
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001 — a malformed hash must not grant access
        return False


def client_ip(request) -> str | None:
    """Real client address, honouring a reverse proxy (nginx, Vercel's edge).

    Stored with the terms acceptance, which is what makes the consent trail
    worth anything under the LGPD.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


# ==========================================================================
#  Management panel: signed session, CSRF, generated passwords
# ==========================================================================
#  Standard library only. The session is a signed cookie, not an encrypted
#  one: anyone can read it, nobody can forge it without SECRET_KEY. It holds
#  the user id, the account's session version and the issue time — nothing
#  that is worth reading.

SESSION_COOKIE = "ccpa_painel"
CSRF_COOKIE = "ccpa_painel_csrf"
SESSION_MAX_AGE = 60 * 60 * 10  # a working day; then sign in again

# In development the key may be left empty: a random one is made per process,
# which only means a restart signs you out. In production it must be set — a
# per-process key on a serverless platform would sign people out at random,
# every time a request lands on a different instance. check.py enforces it.
_DEV_KEY = secrets.token_hex(32)


def _secret() -> bytes:
    from .config import settings

    if settings.secret_key:
        return settings.secret_key.encode()
    if settings.is_prod:
        raise RuntimeError("SECRET_KEY is not configured.")
    return _DEV_KEY.encode()


def _sign(message: str) -> str:
    digest = hmac.new(_secret(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def create_session_token(user_id: str, version: int, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    body = f"{user_id}.{version}.{issued}"
    return f"{body}.{_sign(body)}"


def read_session_token(
    token: str | None, now: float | None = None
) -> tuple[str, int] | None:
    """Return (user id, session version), or None when invalid or expired."""
    if not token or token.count(".") != 3:
        return None
    user_id, version, issued, signature = token.split(".")
    if not hmac.compare_digest(signature, _sign(f"{user_id}.{version}.{issued}")):
        return None
    try:
        age = (now if now is not None else time.time()) - int(issued)
        version_number = int(version)
    except ValueError:
        return None
    if age < 0 or age > SESSION_MAX_AGE:
        return None
    return user_id, version_number


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie: str | None, submitted: str | None) -> bool:
    """Double-submit check: the form must echo the value in the cookie.

    A page on another site can make the browser send our cookie, but cannot
    read it, so it cannot put the same value in the form.
    """
    if not cookie or not submitted:
        return False
    return hmac.compare_digest(cookie, submitted)


# No 0/O, 1/l/I: the password is read aloud or typed from a phone screen.
_READABLE = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_password(length: int = 14) -> str:
    return "".join(secrets.choice(_READABLE) for _ in range(length))
