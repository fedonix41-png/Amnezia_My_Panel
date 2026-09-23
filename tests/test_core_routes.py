"""Contract tests for state-changing routes with the highest blast radius.

External SSH/Docker work is replaced with small fakes.  The HTTP request,
router dispatch, database writes, secret handling, and manager call shapes
remain real so signature drift and partial persistence are caught locally.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select


AUTH_HEADERS = {"Authorization": "Bearer test-only-token"}


class FakeSSH:
    host_fingerprint = "SHA256:test-fingerprint"

    def __init__(self, *args, **kwargs):
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def test_connection(self):
        return "Linux test-host"


async def _allow_admin(db, request):
    return SimpleNamespace(id="test-admin", role="admin", username="admin")


def _isolate_admin_route(monkeypatch):
    import app

    monkeypatch.setattr(app, "_check_admin", _allow_admin)
    monkeypatch.setattr(app, "capture_posthog_event", lambda *args, **kwargs: None)


async def _seed_server(*, with_wireguard=False):
    from database import models
    from database.session import async_session

    async with async_session() as db:
        server = models.Server(
            name="test-server",
            host="203.0.113.10",
            ssh_port=22,
            username="root",
            password="test-password",
            private_key="",
            display_order=0,
        )
        db.add(server)
        await db.commit()
        await db.refresh(server)
        if with_wireguard:
            db.add(
                models.ServerProtocol(
                    server_id=server.id,
                    protocol_type="wireguard",
                    base_protocol="wireguard",
                    port=51820,
                )
            )
            await db.commit()
        return server.id


@pytest.mark.asyncio
async def test_add_server_pins_host_key_and_encrypts_password(app_client, monkeypatch):
    import routers.servers as servers_router
    from database import models
    from database.session import async_session
    from managers.secrets import decrypt_secret

    _isolate_admin_route(monkeypatch)
    monkeypatch.setattr(servers_router, "SSHManager", FakeSSH)

    response = app_client.post(
        "/api/servers/add",
        headers=AUTH_HEADERS,
        json={
            "host": "203.0.113.20",
            "ssh_port": 2222,
            "username": "root",
            "password": "very-secret",
            "name": "edge",
        },
    )

    assert response.status_code == 200, response.json()
    async with async_session() as db:
        server = await db.get(models.Server, response.json()["server_id"])
        assert server is not None
        assert server.password.startswith("v1:")
        assert decrypt_secret(server.password) == "very-secret"
        assert server.host_fingerprint == FakeSSH.host_fingerprint
        assert server.server_info == "Linux test-host"


@pytest.mark.asyncio
async def test_install_wireguard_uses_its_real_signature_and_persists_contract(
    app_client, monkeypatch
):
    import app
    from database import models
    from database.session import async_session

    _isolate_admin_route(monkeypatch)
    server_id = await _seed_server()
    ssh = FakeSSH()
    calls = []

    class WireGuardManager:
        def install_protocol(self, *, port):
            calls.append({"port": port})
            return {"status": "success"}

    monkeypatch.setattr(app, "get_ssh", lambda server, db=None: ssh)
    monkeypatch.setattr(app, "ensure_docker_installed", lambda _ssh: "")
    monkeypatch.setattr(app, "get_protocol_manager", lambda _ssh, protocol: WireGuardManager())

    response = app_client.post(
        f"/api/servers/{server_id}/install",
        headers=AUTH_HEADERS,
        json={"protocol": "wireguard", "port": "51820"},
    )

    assert response.status_code == 200, response.json()
    assert calls == [{"port": "51820"}]
    async with async_session() as db:
        record = (
            await db.execute(
                select(models.ServerProtocol).where(
                    models.ServerProtocol.server_id == server_id,
                    models.ServerProtocol.protocol_type == "wireguard",
                )
            )
        ).scalar_one()
        assert record.port == 51820
        assert record.base_protocol == "wireguard"
        assert record.container_name


@pytest.mark.asyncio
async def test_add_wireguard_connection_uses_special_signature_and_links_user(
    app_client, monkeypatch
):
    import app
    from database import models
    from database.repositories import UserRepository
    from database.session import async_session

    _isolate_admin_route(monkeypatch)
    server_id = await _seed_server(with_wireguard=True)
    async with async_session() as db:
        admin = await UserRepository.get_by_username(db, "admin")
        user_id = admin.id

    calls = []

    class WireGuardManager:
        def add_client(self, name, host):
            calls.append((name, host))
            return {"status": "success", "client_id": "wg-client-1"}

    monkeypatch.setattr(app, "get_ssh", lambda server, db=None: FakeSSH())
    monkeypatch.setattr(app, "get_protocol_manager", lambda _ssh, protocol: WireGuardManager())

    response = app_client.post(
        f"/api/servers/{server_id}/connections/add",
        headers=AUTH_HEADERS,
        json={"protocol": "wireguard", "name": "phone", "user_id": user_id},
    )

    assert response.status_code == 200, response.json()
    assert calls == [("phone", "203.0.113.10")]
    async with async_session() as db:
        connection = (
            await db.execute(
                select(models.Connection).where(
                    models.Connection.server_id == server_id,
                    models.Connection.client_id == "wg-client-1",
                )
            )
        ).scalar_one()
        assert connection.user_id == user_id
        assert connection.protocol == "wireguard"


@pytest.mark.asyncio
async def test_save_settings_persists_nested_configuration_without_starting_bot(
    app_client, monkeypatch
):
    import routers.settings as settings_router
    from database import models
    from database.session import async_session

    _isolate_admin_route(monkeypatch)
    monkeypatch.setattr(settings_router.tg_bot, "is_running", lambda: False)

    payload = {
        "appearance": {"title": "Test Panel", "theme": "dark"},
        "sync": {"remnawave_enabled": False},
        "captcha": {"enabled": True},
        "telegram": {"enabled": False, "token": "", "admin_ids": ""},
        "ssl": {"enabled": False, "panel_port": 5000},
    }
    response = app_client.post(
        "/api/settings/save", headers=AUTH_HEADERS, json=payload
    )

    assert response.status_code == 200, response.json()
    async with async_session() as db:
        settings = await db.get(models.Setting, "settings")
        assert settings.value["appearance"]["title"] == "Test Panel"
        assert settings.value["captcha"]["enabled"] is True
        assert settings.value["telegram"]["enabled"] is False


@pytest.mark.asyncio
async def test_add_user_stores_typed_datetimes_and_a_verifiable_password(
    app_client, monkeypatch
):
    import app
    from database import models
    from database.session import async_session

    _isolate_admin_route(monkeypatch)
    response = app_client.post(
        "/api/users/add",
        headers=AUTH_HEADERS,
        json={
            "username": "alice",
            "password": "correct horse battery staple",
            "role": "user",
            "expiration_date": "2030-01-02T03:04:05+00:00",
        },
    )

    assert response.status_code == 200, response.json()
    async with async_session() as db:
        user = await db.get(models.User, response.json()["user_id"])
        assert isinstance(user.created_at, datetime)
        assert isinstance(user.last_reset_at, datetime)
        assert user.expiration_date.year == 2030
        assert app.verify_password("correct horse battery staple", user.password_hash)
