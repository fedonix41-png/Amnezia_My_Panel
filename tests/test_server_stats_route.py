"""Router contract for POST /api/servers/{id}/stats (WP3.1).

The endpoint must serve the batched host-stats probe through one SSH
command, return the parsed payload unchanged, and degrade to a 500 with a
JSON error when the transport fails — never raise out of the route.
"""

import re
from types import SimpleNamespace

import pytest

import services.server_stats as server_stats

AUTH_HEADERS = {"Authorization": "Bearer test-only-token"}

VALUES = {'cpu': '7.26', 'ram': '250 1000', 'disk': '600 1000',
          'swap': '100 500', 'network': '123456 987654',
          'uptime': 'up 2 days, 3 hours', 'ipv6': '2001:db8::1'}


def _batch_output(command):
    marker = re.search(r'@@HOST_STATS_[0-9a-f]+@@', command)[0]
    return '\n'.join(marker + ':' + name + '\n' + VALUES.get(name, '') + '\n' + marker + ':end:' + name + ':0'
                     for name in server_stats.HOST_STATS_COMMANDS) + '\n' + marker + ':done\n'


async def _allow_admin(db, request):
    return SimpleNamespace(id="test-admin", role="admin", username="admin")


@pytest.fixture
def seeded_server_id(app_client, monkeypatch):
    import app
    from database import models
    from database.session import async_session

    async def _seed():
        async with async_session() as db:
            server = models.Server(
                name="stats-node", host="203.0.113.10", ssh_port=22,
                username="root", password="test-password", private_key="",
                display_order=0,
            )
            db.add(server)
            await db.commit()
            return server.id

    monkeypatch.setattr(app, "_check_admin", _allow_admin)
    monkeypatch.setattr(app, "capture_posthog_event", lambda *a, **kw: None)
    import asyncio
    return asyncio.get_event_loop().run_until_complete(_seed())


def _stats_ssh(monkeypatch, fail=None):
    """Fake SSH answering the batched probe with one framed command."""
    import app

    ssh = SimpleNamespace(commands=[], disconnected=False)

    def connect():
        if fail == 'connect':
            raise OSError('synthetic connect failure')

    def disconnect():
        ssh.disconnected = True

    def run_command(command, timeout=60):
        ssh.commands.append(command)
        if fail == 'command':
            return '', 'synthetic probe failure', 1
        return _batch_output(command), '', 0

    ssh.connect = connect
    ssh.disconnect = disconnect
    ssh.run_command = run_command
    monkeypatch.setattr(app, "get_ssh", lambda *a, **kw: ssh)
    return ssh


def test_stats_route_returns_batched_probe_result(app_client, seeded_server_id, monkeypatch):
    ssh = _stats_ssh(monkeypatch)

    response = app_client.post(
        f"/api/servers/{seeded_server_id}/stats", headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['cpu'] == 7.3
    assert payload['ram_used'] == 250
    assert payload['ram_total'] == 1000
    assert payload['disk_percent'] == 60.0
    assert payload['swap_total'] == 500
    assert payload['net_rx'] == 123456
    assert payload['net_tx'] == 987654
    assert payload['uptime'] == 'up 2 days, 3 hours'
    assert payload['ipv6'] == ['2001:db8::1']
    assert 'errors' not in payload
    # WP3.1 contract: exactly one SSH call instead of ~5 individual probes.
    assert len(ssh.commands) == 1
    assert ssh.commands[0].startswith('command -v bash')
    assert ssh.disconnected is True


def test_stats_route_surfaces_probe_errors_in_payload(app_client, seeded_server_id, monkeypatch):
    ssh = _stats_ssh(monkeypatch, fail='command')

    response = app_client.post(
        f"/api/servers/{seeded_server_id}/stats", headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert 'batch' in payload['errors']
    assert payload['cpu'] == 0
    assert len(ssh.commands) == 1


def test_stats_route_returns_500_when_transport_fails(app_client, seeded_server_id, monkeypatch):
    _stats_ssh(monkeypatch, fail='connect')

    response = app_client.post(
        f"/api/servers/{seeded_server_id}/stats", headers=AUTH_HEADERS)

    assert response.status_code == 500
    assert 'error' in response.json()
