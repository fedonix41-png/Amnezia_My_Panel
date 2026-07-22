"""Custom HTTP middleware: CSRF (double-submit) + security response headers.

Kept in its own module so app.py stays focused on routing. Both middlewares
are ASGI/Starlette ``BaseHTTPMiddleware`` subclasses and compose cleanly with
SessionMiddleware, slowapi, and TrustedHostMiddleware.
"""

from __future__ import annotations

import hmac
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import config

# Methods that must NOT mutate state and are therefore CSRF-exempt.
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

# Paths that are reachable without the panel session (public share surface,
# bootstrap auth) and must not require a CSRF token.
_EXEMPT_PREFIXES = (
    "/api/auth/login",
    "/api/auth/captcha",
    "/share/",
    "/api/share/",
)

_COOKIE_NAME = "csrf_token"

# Minimal CSP sufficient for the current UI (inline scripts/styles are used
# throughout the Jinja templates, so 'unsafe-inline' is required for them;
# tighter policy is a post-MVP goal). frame-ancestors 'none' == X-Frame DENY.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://api.github.com; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


def _is_bearer(request) -> bool:
    auth = request.headers.get("Authorization", "")
    return auth.lower().startswith("bearer ")


def _is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _EXEMPT_PREFIXES)


class CsrfCookieMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF protection.

    On every response without one, sets a non-HttpOnly ``csrf_token`` cookie so
    browser JS can read it and echo it back as ``X-CSRF-Token``. For unsafe
    methods (POST/PUT/PATCH/DELETE) on non-exempt paths, the request is rejected
    unless the header matches the cookie (constant-time compare).

    Bearer-authenticated requests are exempt: API tokens are not automatically
    attached by the browser, so they are not vulnerable to CSRF.
    """

    async def dispatch(self, request, call_next):
        cookie_token = request.cookies.get(_COOKIE_NAME)
        issued = False
        if not cookie_token:
            cookie_token = secrets.token_urlsafe(32)
            issued = True

        if (
            request.method not in _SAFE_METHODS
            and not _is_bearer(request)
            and not _is_exempt(request.url.path)
        ):
            header_token = request.headers.get("X-CSRF-Token", "")
            if not header_token or not hmac.compare_digest(header_token, cookie_token):
                return JSONResponse(
                    {"error": "CSRF token missing or invalid"}, status_code=403
                )

        response = await call_next(request)
        if issued:
            response.set_cookie(
                _COOKIE_NAME,
                cookie_token,
                samesite=config.SESSION_COOKIE_SAMESITE,
                secure=config.SESSION_COOKIE_SECURE,
                httponly=False,
                path="/",
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds defensive headers to every response."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # Modern guidance: disable the legacy auditor; rely on CSP instead.
        response.headers.setdefault("X-XSS-Protection", "0")
        response.headers.setdefault("Content-Security-Policy", _CSP)
        return response
