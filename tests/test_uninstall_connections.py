"""Uninstall purges Connection rows; lists hide phantoms (WP3.7, analog v1.7.2).

Uninstall already deleted UserConnection rows for the (server, protocol)
pair; these tests pin that contract and the belt-side filter that hides
leftover links to uninstalled instances in the users/my connection lists.
"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

AUTH_HEADERS = {"Authorization": "Bearer test-only-token"}


class FakeSSH:
    def __init__(self):
        self.commands = []

    def run_sudo_command(self, cmd, *args, **kwargs):
        self.commands.append(cmd)
        return '', '', 0

    def run_command(self, cmd, *args, **kwargs):
        return '', '', 0

    def connect(self):
        pass

    def disconnect(self):
        pass


class FakeManager:
    def __init__(self):
        self.removed = []

    def remove_container(self, *args):
        self.removed.append(args)


async def _allow_admin(db, request):
    return SimpleNamespace(id="test-admin", role="admin", username="admin")


async def _seed_env():
    """Server + two protocols + admin + live/phantom/dangling connections."""
    from database import models
    from database.session import async_session

    async with async_session() as db:
        server = models.Server(
            name="edge", host="203.0.113.10", ssh_port=22, username="root",
            password="pw", private_key="", display_order=0,
        )
        db.add(server)
        await db.flush()
        db.add(models.ServerProtocol(
            server_id=server.id, protocol_type='awg', port=55424,
            base_protocol='awg', instance=1, display_name='AmneziaWG',
            container_name='amnezia-awg',
        ))
        db.add(models.ServerProtocol(
            server_id=server.id, protocol_type='xray', port=443,
            base_protocol='xray', instance=1, display_name='Xray',
            container_name='amnezia-xray',
        ))
        admin = await db.execute(
            select(models.User).where(models.User.username == 'admin')
        )
        admin = admin.scalar_one()
        rows = [
            # live: awg installed
            models.UserConnection(user_id=admin.id, server_id=server.id,
                                  protocol='awg', client_id='wg-1', name='live-awg'),
            # phantom: xray row exists now but the connection list test
            # removes it to simulate an uninstalled instance
            models.UserConnection(user_id=admin.id, server_id=server.id,
                                  protocol='xray', client_id='xr-1', name='phantom-xray'),
            # live: same protocol on another server would still show, so
            # also pin that uninstall of awg on server A keeps awg on B
        ]
        for row in rows:
            db.add(row)
        await db.commit()
        return server.id, admin.id


@pytest.fixture
def uninstall_env(app_client, monkeypatch):
    import app
    from database import models

    fake = FakeSSH()
    manager = FakeManager()

    async def _drop_xray_protocol():
        from database.session import async_session
        async with async_session() as db:
            res = await db.execute(
                delete(models.ServerProtocol).where(
                    models.ServerProtocol.protocol_type == 'xray'))
            await db.commit()

    monkeypatch.setattr(app, "_check_admin", _allow_admin)
    monkeypatch.setattr(app, "get_ssh", lambda server, db=None: fake)
    monkeypatch.setattr(app, "get_protocol_manager", lambda ssh, protocol: manager)
    monkeypatch.setattr(app, "capture_posthog_event", lambda *a, **k: None)

    loop = asyncio.new_event_loop()
    try:
        server_id, admin_id = loop.run_until_complete(_seed_env())
        loop.run_until_complete(_drop_xray_protocol())
    finally:
        loop.close()

    env = SimpleNamespace(fake=fake, manager=manager, server_id=server_id, admin_id=admin_id)
    return env


def test_uninstall_purges_connections_and_protocol_row(app_client, uninstall_env, monkeypatch):
    import app
    from database import models
    from database.session import async_session
    from sqlalchemy import select

    response = app_client.post(
        f"/api/servers/{uninstall_env.server_id}/uninstall",
        headers=AUTH_HEADERS,
        json={"protocol": "awg"})

    assert response.status_code == 200, response.text
    assert uninstall_env.manager.removed == [('awg',)]

    async def _check():
        async with async_session() as db:
            proto = (await db.execute(select(models.ServerProtocol).where(
                models.ServerProtocol.server_id == uninstall_env.server_id,
                models.ServerProtocol.protocol_type == 'awg'))).scalar_one_or_none()
            assert proto is None
            conns = (await db.execute(select(models.UserConnection).where(
                models.UserConnection.server_id == uninstall_env.server_id))).scalars().all()
            # awg connection purged; the xray phantom remains in the table
            # (only the list endpoints hide it) — purge targets the
            # uninstalled pair only.
            assert sorted(c.protocol for c in conns) == ['xray']

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_check())
    finally:
        loop.close()


async def _get_current_user(db, request):
    from database import models
    from database.session import async_session
    async with async_session() as s:
        res = await s.execute(select(models.User).where(models.User.username == 'admin'))
        return res.scalar_one()


def test_user_connections_hides_phantoms(app_client, uninstall_env, monkeypatch):
    import app
    monkeypatch.setattr(app, "get_current_user", _get_current_user)

    response = app_client.get(
        f"/api/users/{uninstall_env.admin_id}/connections", headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    payload = response.json()
    protocols = sorted(c['protocol'] for c in payload['connections'])
    # xray ServerProtocol was dropped by the fixture: its connection is a
    # phantom and must not be listed; awg is still installed and shows.
    assert protocols == ['awg']


def test_my_connections_hides_phantoms(app_client, uninstall_env, monkeypatch):
    import app
    monkeypatch.setattr(app, "get_current_user", _get_current_user)

    response = app_client.get("/api/my/connections", headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert sorted(c['protocol'] for c in payload['connections']) == ['awg']
