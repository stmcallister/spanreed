"""HTTP basic auth against a single credential.

One user, one password, no accounts and no login page. This sits behind
Tailscale on a homelab, so a session store and a forgotten password flow would
be more moving parts than the thing they protect.

The probe endpoint is exempt. Kubernetes cannot present credentials, and a
readiness probe that always fails is a pod that never becomes ready.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from newsbrief.config import Settings

log = structlog.get_logger(__name__)

EXEMPT_PATHS = frozenset({"/healthz"})

CHALLENGE = {"WWW-Authenticate": 'Basic realm="newsbrief"'}


def credentials_match(header: str | None, user: str, password: str) -> bool:
    """Check an Authorization header against the configured credential.

    Both halves are compared in constant time, and the user name is compared
    even when it cannot match, so a wrong name and a wrong password take the
    same path.
    """
    if not header:
        return False
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False

    supplied_user, separator, supplied_password = decoded.partition(":")
    if not separator:
        return False
    user_ok = secrets.compare_digest(supplied_user, user)
    password_ok = secrets.compare_digest(supplied_password, password)
    return user_ok and password_ok


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Reject every request that does not carry the single credential.

    With no password configured the middleware steps aside and says so at
    startup. The alternative is an application nobody can open, which on a
    private network is the worse failure.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], settings: Settings) -> None:
        super().__init__(app)
        self._user = settings.basic_auth_user
        self._password = settings.basic_auth_password
        if not self._password:
            log.warning(
                "basic auth is not configured, the UI is unauthenticated",
                hint="set BASIC_AUTH_PASSWORD",
            )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if self._password is None or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        header = request.headers.get("Authorization")
        if credentials_match(header, self._user, self._password):
            return await call_next(request)

        return Response(status_code=401, headers=CHALLENGE)
