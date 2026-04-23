import hashlib
import hmac
import secrets
import string
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response

from app.config import get_settings

_SESSION_TOKEN_ALPHABET = set(string.ascii_letters + string.digits + "-_")


@dataclass(frozen=True)
class AnonymousSession:
    token: str
    session_hash: str


def _normalize_session_token(value: str | None) -> str | None:
    if not value:
        return None
    token = value.strip()
    if len(token) < 32 or len(token) > 256:
        return None
    if any(char not in _SESSION_TOKEN_ALPHABET for char in token):
        return None
    return token


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token_for_session(token: str) -> str:
    return hmac.new(
        token.encode("utf-8"),
        b"pdf-accessibility-csrf-v1",
        hashlib.sha256,
    ).hexdigest()


def ensure_anonymous_session(request: Request) -> tuple[AnonymousSession, bool]:
    settings = get_settings()
    token = _normalize_session_token(
        request.cookies.get(settings.anonymous_session_cookie_name)
    )
    created = token is None
    if token is None:
        token = generate_session_token()

    session = AnonymousSession(token=token, session_hash=hash_session_token(token))
    request.state.anonymous_session = session
    return session, created


def set_anonymous_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    max_age = max(1, settings.anonymous_session_cookie_max_age_hours) * 3600
    response.set_cookie(
        key=settings.anonymous_session_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.anonymous_session_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=settings.anonymous_session_csrf_cookie_name,
        value=csrf_token_for_session(token),
        max_age=max_age,
        httponly=False,
        secure=settings.anonymous_session_cookie_secure,
        samesite="lax",
        path="/",
    )


def get_anonymous_session(request: Request) -> AnonymousSession:
    session = getattr(request.state, "anonymous_session", None)
    if isinstance(session, AnonymousSession):
        return session
    raise HTTPException(status_code=500, detail="Anonymous session not initialized")
