"""Integration + unit tests for the API-token subsystem.

Covers the full lifecycle (create / list / revoke), bearer-token
authorization (independent of the session cookie and CSRF), owner-state
enforcement (disabled / downgraded owners lose token access), and the
one-way hashing of raw token values.

The bearer path is CSRF-exempt by design (``middleware._is_bearer``) — API
tokens are not browser-attached so they cannot be CSRF'd. These tests pin
that contract.
"""

import hashlib
import json
import pytest



def _login_as_admin(client):
    """Seed the CSRF cookie and log in as the default admin/admin account.

    Mirrors the pattern in ``tests/test_auth.py``; kept here so this file is
    self-contained and does not cross-import test modules.
    """
    client.get("/login")
    csrf = client.cookies.get("csrf_token", "")
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.json()


def _create_token(client, name="ci"):
    """Create a token as the logged-in admin and return the raw value + id."""
    csrf = client.cookies.get("csrf_token", "")
    r = client.post(
        "/api/settings/tokens",
        json={"name": name},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.json()
    body = r.json()
    return body["token"], body["id"]


def _drop_session(client):
    """Clear the session cookie so ``_check_admin`` can only authenticate via
    the Bearer header — the contract these bearer-path tests pin."""
    client.cookies.delete("session")


class TestApiTokenLifecycle:
    def test_create_token_returns_raw_once(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        _login_as_admin(app_client)
        raw, token_id = _create_token(app_client, name="ci")

        assert raw.startswith("awp_")
        # ~256 bits of entropy beyond the prefix: at least 32 chars.
        assert len(raw) > len("awp_") + 32
        assert token_id

    def test_list_tokens_does_not_expose_raw(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        _login_as_admin(app_client)
        raw, _ = _create_token(app_client, name="ci")

        r = app_client.get("/api/settings/tokens")
        assert r.status_code == 200
        tokens = r.json()["tokens"]
        assert len(tokens) == 1
        entry = tokens[0]
        # The sensitive fields must never be exposed by the list endpoint.
        assert "token" not in entry
        assert "token_hash" not in entry
        # The identifying prefix is safe to surface (prefix + 4 chars).
        assert entry["token_prefix"] == raw[: len("awp_") + 4]
        assert entry["name"] == "ci"
        assert entry["owner"] == "admin"
        assert entry["created_at"]

    def test_revoke_token(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        _login_as_admin(app_client)
        _, token_id = _create_token(app_client, name="ci")

        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.delete(
            f"/api/settings/tokens/{token_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200

        # Second revoke of the same id → 404 (already gone).
        r2 = app_client.delete(
            f"/api/settings/tokens/{token_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert r2.status_code == 404

    def test_revoke_unknown_returns_404(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        _login_as_admin(app_client)
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.delete(
            "/api/settings/tokens/no-such-id",
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 404

    def test_empty_name_rejected(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        _login_as_admin(app_client)
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post(
            "/api/settings/tokens",
            json={"name": ""},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 400


class TestBearerAuth:
    """Bearer-token requests must work without a session cookie and without
    a CSRF header — the token itself is the credential."""

    def test_bearer_grants_admin_access(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        _login_as_admin(app_client)
        raw, _ = _create_token(app_client, name="ci")
        _drop_session(app_client)

        # No X-CSRF-Token, no session cookie — the bearer header alone must
        # authenticate. Dropping the session is what isolates this from a
        # false pass: _check_admin otherwise returns on the cookie branch
        # before the bearer is ever evaluated.
        r = app_client.get(
            "/api/settings/tokens",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200

    def test_bearer_revoked_after_delete(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        _login_as_admin(app_client)
        raw, token_id = _create_token(app_client, name="ci")

        csrf = app_client.cookies.get("csrf_token", "")
        app_client.delete(
            f"/api/settings/tokens/{token_id}",
            headers={"X-CSRF-Token": csrf},
        )
        _drop_session(app_client)

        r = app_client.get(
            "/api/settings/tokens",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 403

    def test_bearer_unknown_returns_403(self, app_client):
        # An unrecognised bearer must be rejected cleanly, not crash with 500.
        r = app_client.get(
            "/api/settings/tokens",
            headers={"Authorization": "Bearer awp_nope_not_a_real_token"},
        )
        assert r.status_code == 403

    def test_bearer_token_prefix_only_rejected(self, app_client):
        import app as appmod

        appmod.limiter.reset()
        _login_as_admin(app_client)
        raw, _ = _create_token(app_client, name="ci")
        prefix = raw[: len("awp_") + 4]
        _drop_session(app_client)

        # The token_prefix is intentionally shorter than the raw token and
        # must NOT authenticate on its own.
        r = app_client.get(
            "/api/settings/tokens",
            headers={"Authorization": f"Bearer {prefix}"},
        )
        assert r.status_code == 403


class TestTokenOwnerState:
    """A token's authority is derived from its owner. If the owner is later
    disabled or demoted, the token must stop working immediately."""

    @pytest.mark.asyncio
    async def test_disabled_owner_token_stops_working(self, app_client):
        import app as appmod
        from database.session import async_session
        from database.repositories import UserRepository

        appmod.limiter.reset()
        _login_as_admin(app_client)
        raw, _ = _create_token(app_client, name="ci")
        _drop_session(app_client)

        async with async_session() as db:
            admin = await UserRepository.get_by_username(db, "admin")
            if admin:
                await UserRepository.update(db, admin.id, {"enabled": False})

        r = app_client.get(
            "/api/settings/tokens",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 403

        async with async_session() as db:
            admin = await UserRepository.get_by_username(db, "admin")
            if admin:
                await UserRepository.update(db, admin.id, {"enabled": True})

    @pytest.mark.asyncio
    async def test_downgraded_owner_token_stops_working(self, app_client):
        import app as appmod
        from database.session import async_session
        from database.repositories import UserRepository

        appmod.limiter.reset()
        _login_as_admin(app_client)
        raw, _ = _create_token(app_client, name="ci")
        _drop_session(app_client)

        async with async_session() as db:
            admin = await UserRepository.get_by_username(db, "admin")
            if admin:
                await UserRepository.update(db, admin.id, {"role": "user"})

        r = app_client.get(
            "/api/settings/tokens",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 403

        async with async_session() as db:
            admin = await UserRepository.get_by_username(db, "admin")
            if admin:
                await UserRepository.update(db, admin.id, {"role": "admin"})



class TestTokenHashing:
    """Unit tests on the hashing primitive — no HTTP, no fixtures."""

    def test_hash_is_sha256_hex(self):
        from app import _hash_api_token

        h = _hash_api_token("awp_test_value")
        assert len(h) == 64
        # Must be a lowercase hex digest (SHA-256 of the UTF-8 bytes).
        assert h == hashlib.sha256(b"awp_test_value").hexdigest()
        int(h, 16)  # raises if not hex

    def test_hash_is_one_way(self):
        from app import _hash_api_token

        a = _hash_api_token("awp_one")
        b = _hash_api_token("awp_two")
        assert a != b
        # The raw value must not appear inside the digest.
        assert "awp_one" not in a
        # Two different inputs must not collide (sanity for the algorithm).
        assert _hash_api_token("awp_one") == _hash_api_token("awp_one")



