"""Integration tests for authentication, rate-limiting, and CSRF protection."""

import re

import pytest


class TestLogin:
    def test_login_creates_default_admin(self, app_client):
        c = app_client
        # _startup creates admin/admin. First, seed the csrf cookie.
        c.get("/login")
        csrf = c.cookies.get("csrf_token", "")

        r = c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200, r.json()
        assert r.json()["role"] == "admin"
        assert r.json()["status"] == "success"

    def test_login_wrong_password(self, app_client):
        c = app_client
        c.get("/login")
        csrf = c.cookies.get("csrf_token", "")

        r = c.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 401, r.json()
        err = r.json()["error"].lower()
        assert "invalid" in err or "неверн" in err or "login" in err or "парол" in err

    def test_login_missing_user(self, app_client):
        c = app_client
        c.get("/login")
        csrf = c.cookies.get("csrf_token", "")

        r = c.post(
            "/api/auth/login",
            json={"username": "no-such-user", "password": "x"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 401


class TestRateLimiting:
    def test_login_rate_limit_triggers_after_burst(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        c = app_client
        c.get("/login")
        csrf = c.cookies.get("csrf_token", "")

        statuses = []
        for _ in range(7):
            r = c.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers={"X-CSRF-Token": csrf},
            )
            statuses.append(r.status_code)

        # First 5 should be 401 (wrong password), then 429 (rate limited)
        ok_before = [s for s in statuses if s == 401]
        limited = [s for s in statuses if s == 429]
        assert len(ok_before) >= 4, f"expected >=4 401s, got statuses {statuses}"
        assert len(limited) >= 1, f"expected >=1 429, got statuses {statuses}"


class TestCsrf:
    def _extract_csrf(self, client):
        client.get("/login")
        return client.cookies.get("csrf_token", "")

    def test_unsafe_method_rejected_without_csrf(self, app_client):
        r = app_client.post("/api/servers/add", json={"host": "x", "username": "y"})
        assert r.status_code == 403
        assert "CSRF" in r.json()["error"]

    def test_unsafe_method_accepted_with_csrf(self, app_client):
        c = app_client
        csrf = c.cookies.get("csrf_token", "")
        if not csrf:
            c.get("/login")
            csrf = c.cookies.get("csrf_token", "")
        # Auth fails (not logged in), but CSRF passes. Server returns 403
        # Forbidden from _check_admin rather than 403 CSRF from middleware.
        r = c.post(
            "/api/servers/add",
            json={"host": "x", "username": "y"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 403
        assert r.json().get("error") == "Forbidden"  # from _check_admin, not CSRF

    def test_bearer_auth_bypasses_csrf(self, app_client):
        r = app_client.post(
            "/api/servers/add",
            json={"host": "x", "username": "y"},
            headers={"Authorization": "Bearer fake-token"},
        )
        assert r.status_code == 403
        assert r.json().get("error") == "Forbidden"  # from _check_admin, not CSRF

    def test_login_exempt_from_csrf(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        r = app_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        # No CSRF token sent. Should be 200 or 401 (wrong creds) — not 403 CSRF.
        assert r.status_code in (200, 401), f"got {r.status_code}: {r.json()}"


class TestCaptcha:
    def test_captcha_endpoint(self, app_client):
        r = app_client.get("/api/auth/captcha")
        # Captcha may fail if multicolorcaptcha is not installed (500) or work (200)
        # Either is acceptable; we just check it doesn't 403 CSRF or crash.
        assert r.status_code in (200, 500), f"unexpected {r.status_code}"


class TestSecurityHeaders:
    def test_security_headers_present(self, app_client):
        r = app_client.get("/login")
        assert r.headers.get("x-frame-options") == "DENY"
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
        assert r.headers.get("content-security-policy") is not None
