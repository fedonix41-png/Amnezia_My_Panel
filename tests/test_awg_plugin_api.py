"""Router contract for the AWG plugin updater API (WP3.2).

Manager-level behavior (status/update/rollback on a fake SSH) is covered in
tests/managers/test_awg_plugin_*.py; these tests pin the HTTP layer:
authorization, component validation, in-progress 409, operation lifecycle
and PluginUpdateError mapping — with the updater itself replaced by a fake.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

AUTH_HEADERS = {"Authorization": "Bearer test-only-token"}


class FakeUpdater:
    """Instant stand-in for AwgPluginUpdater."""

    def __init__(self, fail_status=False):
        self.fail_status = fail_status
        self.updates = []

    def status(self):
        if self.fail_status:
            from managers.awg_plugin_updater import PluginUpdateError
            raise PluginUpdateError('transport', 'synthetic SSH failure')
        return {
            'kernel_module': {'installed': '3.1.20260812', 'latest': '3.1.20260906'},
            'userspace': {'installed': '3.1.20260906', 'latest': '3.1.20260906'},
        }

    def changelog(self, component, version):
        return [f'{component} {version}: synthetic entry']

    def latest_versions(self):
        return {'kernel_module': '3.1.20260906', 'userspace': '3.1.20260906'}

    def update(self, component, target, allow_interrupt=False, log=None, should_cancel=None):
        self.updates.append((component, target, allow_interrupt))
        if log:
            log('start', 'synthetic update start')
            log('done', 'synthetic update done')
        return {'state': 'success', 'component': component, 'target': target}


async def _allow_admin(db, request):
    return SimpleNamespace(id="test-admin", role="admin", username="admin")


async def _deny_admin(db, request):
    return None


@pytest.fixture
def updater_env(app_client, monkeypatch, tmp_path):
    """Isolated ops registry + fake updater + seeded server."""
    import app
    import routers.awg_plugin as awg_plugin_router
    from database import models
    from database.session import async_session

    fake = FakeUpdater()

    async def fake_updater(server_id, db):
        return fake

    async def _seed():
        async with async_session() as db:
            server = models.Server(
                name="plugin-node", host="203.0.113.10", ssh_port=22,
                username="root", password="test-password", private_key="",
                display_order=0,
            )
            db.add(server)
            await db.commit()
            return server.id

    monkeypatch.setattr(app, "_check_admin", _allow_admin)
    monkeypatch.setattr(app, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setattr(awg_plugin_router, "_awg_plugin_updater", fake_updater)
    awg_plugin_router._awg_plugin_ops.clear()
    awg_plugin_router._awg_plugin_active.clear()
    awg_plugin_router._awg_plugin_loaded['done'] = False
    # Sync tests run outside a loop: seed through the session on a fresh loop.
    loop = asyncio.new_event_loop()
    try:
        server_id = loop.run_until_complete(_seed())
    finally:
        loop.close()
    return SimpleNamespace(fake=fake, server_id=server_id)


def _drive_op(op_id, server_id, component, target, allow_interrupt=False):
    """Run the background pipeline deterministically.

    TestClient tears down its request-scoped task group when the response is
    sent, which cancels tasks spawned via asyncio.create_task (the conftest
    app_client deliberately avoids the lifespan portal that would keep them
    alive). Driving _run_awg_plugin_update on a fresh loop keeps the HTTP
    layer honest while making the operation outcome deterministic.
    """
    import routers.awg_plugin as awg_plugin_router
    asyncio.run(awg_plugin_router._run_awg_plugin_update(
        op_id, server_id, component, target, allow_interrupt))


def _wait_op(app_client, server_id, op_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = app_client.get(
            f"/api/servers/{server_id}/awg-plugin/update/{op_id}",
            headers=AUTH_HEADERS)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload['state'] != 'running':
            return payload
        time.sleep(0.05)
    raise AssertionError("update operation did not finish in time")


def test_status_requires_admin(app_client, updater_env, monkeypatch):
    import app
    monkeypatch.setattr(app, "_check_admin", _deny_admin)
    response = app_client.get(
        f"/api/servers/{updater_env.server_id}/awg-plugin/status",
        headers=AUTH_HEADERS)
    assert response.status_code == 403


def test_status_returns_component_versions(app_client, updater_env):
    response = app_client.get(
        f"/api/servers/{updater_env.server_id}/awg-plugin/status",
        headers=AUTH_HEADERS)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['kernel_module']['installed'] == '3.1.20260812'
    assert payload['kernel_module']['latest'] == '3.1.20260906'


def test_status_maps_plugin_update_error(app_client, updater_env, monkeypatch):
    updater_env.fake.fail_status = True
    response = app_client.get(
        f"/api/servers/{updater_env.server_id}/awg-plugin/status",
        headers=AUTH_HEADERS)
    assert response.status_code == 400
    assert response.json()['code'] == 'transport'


def test_status_unknown_server(app_client, updater_env, monkeypatch):
    import routers.awg_plugin as awg_plugin_router
    from managers.awg_plugin_updater import PluginUpdateError

    async def missing_updater(server_id, db):
        raise PluginUpdateError('server_not_found', 'Server not found')

    monkeypatch.setattr(awg_plugin_router, "_awg_plugin_updater", missing_updater)
    response = app_client.get(
        "/api/servers/9999/awg-plugin/status", headers=AUTH_HEADERS)
    assert response.status_code == 502
    assert response.json()['code'] == 'server_not_found'


def test_changelog_validates_component(app_client, updater_env):
    response = app_client.get(
        f"/api/servers/{updater_env.server_id}/awg-plugin/changelog",
        params={'component': 'bogus', 'version': '3.1.20260906'},
        headers=AUTH_HEADERS)
    assert response.status_code == 400
    assert response.json()['code'] == 'unsupported'


def test_changelog_returns_entries(app_client, updater_env):
    response = app_client.get(
        f"/api/servers/{updater_env.server_id}/awg-plugin/changelog",
        params={'component': 'kernel_module', 'version': '3.1.20260906'},
        headers=AUTH_HEADERS)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['component'] == 'kernel_module'
    assert payload['entries'] == ['kernel_module 3.1.20260906: synthetic entry']


def test_update_validates_component(app_client, updater_env):
    response = app_client.post(
        f"/api/servers/{updater_env.server_id}/awg-plugin/update",
        headers=AUTH_HEADERS,
        json={'component': 'bogus'})
    assert response.status_code == 400
    assert response.json()['code'] == 'unsupported'


def test_update_runs_operation_to_success(app_client, updater_env):
    response = app_client.post(
        f"/api/servers/{updater_env.server_id}/awg-plugin/update",
        headers=AUTH_HEADERS,
        json={'component': 'kernel_module', 'target': '3.1.20260906'})
    assert response.status_code == 200, response.text
    op_id = response.json()['operation_id']

    _drive_op(op_id, updater_env.server_id, 'kernel_module', '3.1.20260906')

    payload = _wait_op(app_client, updater_env.server_id, op_id)
    assert payload['state'] == 'success'
    assert payload['target'] == '3.1.20260906'
    assert payload['result']['state'] == 'success'
    assert payload['step'] > 0
    assert payload['total'] == 12
    assert payload['percent'] == 100
    stages = [line['stage'] for line in payload['lines']]
    assert 'start' in stages and 'done' in stages
    # The endpoint's background task may also reach FakeUpdater.update before
    # TestClient cancels it; _drive_op's run is the deterministic last entry.
    assert updater_env.fake.updates[-1] == ('kernel_module', '3.1.20260906', False)


def test_update_rejects_concurrent_run(app_client, updater_env, monkeypatch):
    import routers.awg_plugin as awg_plugin_router
    # Seed an in-flight operation for the same (server, component) pair.
    monkeypatch.setattr(awg_plugin_router, "_awg_plugin_active", {(updater_env.server_id, 'kernel_module'): 'existing'})
    response = app_client.post(
        f"/api/servers/{updater_env.server_id}/awg-plugin/update",
        headers=AUTH_HEADERS,
        json={'component': 'kernel_module'})
    assert response.status_code == 409
    assert response.json()['code'] == 'update_in_progress'


def test_operation_status_unknown_id(app_client, updater_env):
    response = app_client.get(
        f"/api/servers/{updater_env.server_id}/awg-plugin/update/nope",
        headers=AUTH_HEADERS)
    assert response.status_code == 404


def test_cancel_unknown_operation(app_client, updater_env):
    response = app_client.post(
        f"/api/servers/{updater_env.server_id}/awg-plugin/update/nope/cancel",
        headers=AUTH_HEADERS)
    assert response.status_code == 404


def test_cancel_finished_operation_conflicts(app_client, updater_env):
    response = app_client.post(
        f"/api/servers/{updater_env.server_id}/awg-plugin/update",
        headers=AUTH_HEADERS,
        json={'component': 'userspace'})
    op_id = response.json()['operation_id']
    _drive_op(op_id, updater_env.server_id, 'userspace', None)
    payload = _wait_op(app_client, updater_env.server_id, op_id)
    assert payload['state'] == 'success'

    response = app_client.post(
        f"/api/servers/{updater_env.server_id}/awg-plugin/update/{op_id}/cancel",
        headers=AUTH_HEADERS)
    assert response.status_code == 409
    assert response.json()['code'] == 'not_running'


def test_operation_lines_support_offset(app_client, updater_env):
    response = app_client.post(
        f"/api/servers/{updater_env.server_id}/awg-plugin/update",
        headers=AUTH_HEADERS,
        json={'component': 'kernel_module'})
    op_id = response.json()['operation_id']
    _drive_op(op_id, updater_env.server_id, 'kernel_module', None)
    payload = _wait_op(app_client, updater_env.server_id, op_id)
    next_offset = payload['next_offset']

    response = app_client.get(
        f"/api/servers/{updater_env.server_id}/awg-plugin/update/{op_id}",
        params={'offset': next_offset},
        headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json()['lines'] == []
    assert response.json()['next_offset'] == next_offset
