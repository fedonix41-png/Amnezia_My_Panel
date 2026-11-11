"""Router contract for protocol backup upload/restore/delete (WP3.4).

The manager behavior on a fake SSH is covered in tests/managers/test_backup_*.py;
these tests pin the HTTP layer: authorization, protocol/filename validation,
size caps, and error mapping — with the real BackupManager running against a
fake SSH so the integration path stays honest.
"""

import asyncio
import io
import json
import os
import shutil
import tarfile
from types import SimpleNamespace

import pytest

AUTH_HEADERS = {"Authorization": "Bearer test-only-token"}


class FakeSFTP:
    def __init__(self, ssh):
        self._ssh = ssh

    def get(self, remote, local):
        src = self._ssh.sftp_sources.get(os.path.basename(remote))
        if src is None:
            raise IOError('No such remote file: %s' % remote)
        shutil.copyfile(src, local)

    def put(self, local, remote):
        dst = os.path.join(self._ssh.sftp_put_dir, os.path.basename(remote))
        shutil.copyfile(local, dst)

    def close(self):
        pass


class _Client:
    def __init__(self, ssh):
        self._ssh = ssh

    def open_sftp(self):
        return FakeSFTP(self._ssh)


class FakeSSH:
    def __init__(self, *, command_code=0, script_code=0):
        self.commands = []
        self.scripts = []
        self.command_code = command_code
        self.script_code = script_code
        self.command_handler = None
        self.sftp_sources = {}
        self.sftp_put_dir = None
        self.client = _Client(self)

    def run_sudo_command(self, command, timeout=60):
        self.commands.append(command)
        if self.command_handler is not None:
            return self.command_handler(command)
        return '', '', self.command_code

    def run_sudo_script(self, script, timeout=120):
        self.scripts.append(script)
        return '', '', self.script_code

    def connect(self):
        import tempfile
        self.sftp_put_dir = tempfile.mkdtemp(prefix='amnezia-test-sftp-')

    def disconnect(self):
        pass


async def _allow_admin(db, request):
    return SimpleNamespace(id="test-admin", role="admin", username="admin")


async def _deny_admin(db, request):
    return None


@pytest.fixture
def backup_env(app_client, monkeypatch, tmp_path):
    """Seeded server + fake SSH + admin auth."""
    import app
    from database import models
    from database.session import async_session

    fake = FakeSSH()

    async def _seed():
        async with async_session() as db:
            server = models.Server(
                name="backup-node", host="203.0.113.10", ssh_port=22,
                username="root", password="test-password", private_key="",
                display_order=0,
            )
            db.add(server)
            await db.commit()
            return server.id

    monkeypatch.setattr(app, "_check_admin", _allow_admin)
    monkeypatch.setattr(app, "get_ssh", lambda server, db=None: fake)
    loop = asyncio.new_event_loop()
    try:
        server_id = loop.run_until_complete(_seed())
    finally:
        loop.close()
    env = SimpleNamespace(fake=fake, server_id=server_id, tmp_path=tmp_path)
    return env


def _archive_bytes(protocol='awg3'):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        data = json.dumps({
            'protocol': protocol,
            'backup_format': 'amnezia-web-panel-protocol-v1',
        }).encode('utf-8')
        info = tarfile.TarInfo('backup-info.json')
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# ---------- permission ----------

@pytest.mark.parametrize("method,url", [
    ("post", "/api/servers/{sid}/backups/upload"),
    ("post", "/api/servers/{sid}/backups/restore"),
    ("post", "/api/servers/{sid}/backups/delete"),
])
def test_non_admin_forbidden(app_client, backup_env, monkeypatch, method, url):
    import app
    monkeypatch.setattr(app, "_check_admin", _deny_admin)
    target = url.format(sid=backup_env.server_id)
    if method == "post" and "upload" in url:
        response = app_client.post(target, headers=AUTH_HEADERS,
                                   data={'protocol': 'awg3'},
                                   files={'file': ('a.tar.gz', b'x', 'application/gzip')})
    else:
        response = app_client.post(target, headers=AUTH_HEADERS,
                                   json={'protocol': 'awg3', 'filename': 'a.tar.gz'})
    assert response.status_code == 403


# ---------- upload ----------

def test_upload_rejects_unknown_protocol(app_client, backup_env):
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/upload",
        headers=AUTH_HEADERS,
        data={'protocol': 'bogus'},
        files={'file': ('a.tar.gz', _archive_bytes(), 'application/gzip')})
    assert response.status_code == 400


def test_upload_rejects_empty_file(app_client, backup_env):
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/upload",
        headers=AUTH_HEADERS,
        data={'protocol': 'awg3'},
        files={'file': ('empty.tar.gz', b'', 'application/gzip')})
    assert response.status_code == 400
    assert response.json()['error'] == 'Empty file'


def test_upload_rejects_oversized_file(app_client, backup_env, monkeypatch):
    from managers.backup_manager import BackupManager
    monkeypatch.setattr(BackupManager, 'MAX_UPLOAD_BYTES', 16)
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/upload",
        headers=AUTH_HEADERS,
        data={'protocol': 'awg3'},
        files={'file': ('big.tar.gz', b'x' * 64, 'application/gzip')})
    assert response.status_code == 413


def test_upload_rejects_server_not_found(app_client, backup_env):
    response = app_client.post(
        "/api/servers/999999/backups/upload",
        headers=AUTH_HEADERS,
        data={'protocol': 'awg3'},
        files={'file': ('a.tar.gz', _archive_bytes(), 'application/gzip')})
    assert response.status_code == 404


def test_upload_rejects_invalid_archive(app_client, backup_env):
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/upload",
        headers=AUTH_HEADERS,
        data={'protocol': 'awg3'},
        files={'file': ('broken.tar.gz', b'not an archive', 'application/gzip')})
    assert response.status_code == 400


def test_upload_success_stores_archive(app_client, backup_env):
    backup_env.fake.command_handler = lambda c: ('', '', 1 if c.startswith('test -f') else 0)
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/upload",
        headers=AUTH_HEADERS,
        data={'protocol': 'awg3'},
        files={'file': ('awg3-20260101.tar.gz', _archive_bytes(), 'application/gzip')})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['status'] == 'success'
    assert payload['backup']['name'] == 'awg3-20260101.tar.gz'
    assert payload['backup']['path'].startswith('/opt/amnezia/backups/awg3/')


# ---------- restore ----------

def test_restore_rejects_unknown_protocol(app_client, backup_env):
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/restore",
        headers=AUTH_HEADERS,
        json={'protocol': 'bogus', 'filename': 'a.tar.gz'})
    assert response.status_code == 400


def test_restore_rejects_invalid_filename(app_client, backup_env):
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/restore",
        headers=AUTH_HEADERS,
        json={'protocol': 'awg3', 'filename': '../../etc/passwd'})
    assert response.status_code == 400


def test_restore_rejects_server_not_found(app_client, backup_env):
    response = app_client.post(
        "/api/servers/999999/backups/restore",
        headers=AUTH_HEADERS,
        json={'protocol': 'awg3', 'filename': 'awg3-20260101.tar.gz'})
    assert response.status_code == 404


def test_restore_manager_error_maps_to_500(app_client, backup_env):
    backup_env.fake.command_code = 1  # fetch `sh -c` probe fails -> not found
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/restore",
        headers=AUTH_HEADERS,
        json={'protocol': 'awg3', 'filename': 'awg3-20260101.tar.gz'})
    assert response.status_code == 500
    assert 'error' in response.json()


def test_restore_success_runs_pipeline(app_client, backup_env):
    seed = backup_env.tmp_path / 'seed.tar.gz'
    seed.write_bytes(_archive_bytes())
    backup_env.fake.sftp_sources['_amnz_validate_awg3-20260101.tar.gz'] = str(seed)
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/restore",
        headers=AUTH_HEADERS,
        json={'protocol': 'awg3', 'filename': 'awg3-20260101.tar.gz'})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['status'] == 'success'
    assert payload['filename'] == 'awg3-20260101.tar.gz'
    assert len(backup_env.fake.scripts) == 1
    assert 'amnezia-restore-' in backup_env.fake.scripts[0]


# ---------- delete ----------

def test_delete_rejects_unknown_protocol(app_client, backup_env):
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/delete",
        headers=AUTH_HEADERS,
        json={'protocol': 'bogus', 'filename': 'a.tar.gz'})
    assert response.status_code == 400


def test_delete_rejects_invalid_filename(app_client, backup_env):
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/delete",
        headers=AUTH_HEADERS,
        json={'protocol': 'awg3', 'filename': 'no-extension'})
    assert response.status_code == 400


def test_delete_rejects_server_not_found(app_client, backup_env):
    response = app_client.post(
        "/api/servers/999999/backups/delete",
        headers=AUTH_HEADERS,
        json={'protocol': 'awg3', 'filename': 'awg3-20260101.tar.gz'})
    assert response.status_code == 404


def test_delete_manager_error_maps_to_404(app_client, backup_env):
    backup_env.fake.command_code = 1
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/delete",
        headers=AUTH_HEADERS,
        json={'protocol': 'awg3', 'filename': 'awg3-20260101.tar.gz'})
    assert response.status_code == 404


def test_delete_success(app_client, backup_env):
    response = app_client.post(
        f"/api/servers/{backup_env.server_id}/backups/delete",
        headers=AUTH_HEADERS,
        json={'protocol': 'awg3', 'filename': 'awg3-20260101.tar.gz'})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['status'] == 'success'
    assert payload['filename'] == 'awg3-20260101.tar.gz'
    assert backup_env.fake.commands[0].startswith('bash -c ')
