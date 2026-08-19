"""Hardening that ships switched off.

The default deployment is loopback-only and needs none of this. It is built in
anyway so that exposing the service later is a `.env` edit rather than a
rewrite: set `GUI_AUTH_TOKEN` to require a shared secret, and
`GUI_RATE_LIMIT_PER_MIN` to throttle per client address.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

SESSION_COOKIE = "wr_gui_session"

#: Everything is served from our own origin; no CDN, no inline script or style.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

#: Reachable without a session when auth is on: the shell, its assets, the
#: liveness probe and the login call itself.
_PUBLIC_PATHS = {"/", "/index.html", "/api/login", "/api/ping", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/static/",)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    """Shared-secret gate. Inactive when the token is empty."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if not self._token:
            return await call_next(request)
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)
        if not self.authorised(request, self._token):
            return JSONResponse(
                {"error": "unauthorised", "detail": "This instance requires an access token."},
                status_code=401,
            )
        return await call_next(request)

    @staticmethod
    def authorised(request: Request, token: str) -> bool:
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if cookie and secrets.compare_digest(cookie, token):
            return True
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return secrets.compare_digest(header[7:].strip(), token)
        return False


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window limit per client address, applied to /api/ only."""

    def __init__(self, app, per_minute: int) -> None:
        super().__init__(app)
        self._limit = per_minute
        self._window = 60.0
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if self._limit <= 0 or not request.url.path.startswith("/api/"):
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        bucket = self._hits[client]
        while bucket and now - bucket[0] > self._window:
            bucket.popleft()
        if len(bucket) >= self._limit:
            retry_after = max(1, int(self._window - (now - bucket[0])))
            return JSONResponse(
                {
                    "error": "rate_limited",
                    "detail": f"Too many requests. Try again in {retry_after}s.",
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
        # Keep the table from growing forever on a long-lived process.
        if len(self._hits) > 4096:
            for key in [k for k, v in self._hits.items() if not v]:
                del self._hits[key]
        return await call_next(request)
