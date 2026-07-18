"""Single-user auth: password login -> signed session cookie, or bearer token.

The dashboard may be exposed on a VPS, so everything except /healthz and
/login requires auth. Configure DASHBOARD_PASSWORD (+ SECRET_KEY!) in .env;
optionally DASHBOARD_TOKEN for API/scripting access.
"""

from __future__ import annotations

import asyncio
import hmac
import logging

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import get_settings

log = logging.getLogger(__name__)

COOKIE_NAME = "tcg_session"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="tcg-tracker-session")


def create_session_cookie() -> str:
    return _serializer().dumps({"user": "admin"})


def verify_session_cookie(value: str) -> bool:
    try:
        _serializer().loads(value, max_age=get_settings().session_max_age_seconds)
        return True
    except BadSignature:
        return False


async def verify_password(password: str) -> bool:
    await asyncio.sleep(0.3)  # cheap brute-force damping
    return hmac.compare_digest(password, get_settings().dashboard_password)


def is_authenticated(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and verify_session_cookie(cookie):
        return True
    token = get_settings().dashboard_token
    auth_header = request.headers.get("Authorization", "")
    if token and auth_header.startswith("Bearer "):
        return hmac.compare_digest(auth_header.removeprefix("Bearer ").strip(), token)
    return False


async def require_auth(request: Request) -> None:
    """Dependency for HTML routes: redirect to /login when unauthenticated."""
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/login?next={request.url.path}"},
        )


RequireAuth = Depends(require_auth)
