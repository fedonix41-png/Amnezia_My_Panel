"""BackupManager restore/upload/delete on a fake SSH (WP3.4).

Pins the manager half of the port: filename validation, single-shell sudo
chains, upload protocol check, and the restore fetch -> inspect -> script
pipeline. Archive layout validation lives in test_backup_archive_validation.py.
"""

import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest

from managers.backup_manager import BackupManager


class FakeSFTP:
    def __init__(self, ssh):
        self._ssh = ssh

    def get(self, remote, local):
        src = self._ssh.sftp_sources.get(os.path.basename(remote))
        if src is None:
            raise IOError('No such remote file: %s' % remote)
        shutil.copyfile(src, local)
        self._ssh.transfers.append(('get', remote, local))

    def put(self, local, remote):
        dst = os.path.join(self._ssh.sftp_put_dir, os.path.basename(remote))
        shutil.copyfile(local, dst)
        self._ssh.transfers.append(('put', local, remote, dst))

    def close(self):
        pass


class _Client:
    def __init__(self, ssh):
        self._ssh = ssh

    def open_sftp(self):
        return FakeSFTP(self._ssh)


class FakeSSH:
    """Records commands/scripts; canned results; SFTP backed by local files."""

    def __init__(self, *, command_code=0, command_out='', script_code=0, script_out='',
                 command_handler=None):
        self.commands = []
        self.scripts = []
        self.command_code = command_code
        self.command_out = command_out
        self.script_code = script_code
        self.script_out = script_out
        self.command_handler = command_handler
        self.sftp_sources = {}
        self.sftp_put_dir = tempfile.mkdtemp(prefix='amnezia-test-sftp-')
        self.transfers = []
        self.client = _Client(self)

    def run_sudo_command(self, command, timeout=60):
        self.commands.append(command)
        if self.command_handler is not None:
            return self.command_handler(command)
        return self.command_out, '', self.command_code

    def run_sudo_script(self, script, timeout=120):
        self.scripts.append((script, timeout))
        return self.script_out, '', self.script_code


def _make_archive(path, protocol='awg3'):
    with tarfile.open(path, 'w:gz') as archive:
        data = json.dumps({
            'protocol': protocol,
            'backup_format': BackupManager.BACKUP_FORMAT,
        }).encode('utf-8')
        info = tarfile.TarInfo('backup-info.json')
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return path


class ListBackupsTests(unittest.TestCase):
    def test_pipeline_runs_inside_one_bash_shell(self):
        ssh = FakeSSH(command_out='awg3-20260101T000000Z.tar.gz|1024|1767225600.0\n')
        result = BackupManager(ssh).list_backups('awg3')
        self.assertEqual(result['status'], 'success')
        self.assertEqual(len(ssh.commands), 1)
        cmd = ssh.commands[0]
        self.assertTrue(cmd.startswith('bash -c '), cmd)
        self.assertIn('mkdir -p', cmd)
        self.assertIn('find', cmd)
        self.assertEqual(result['backups'][0]['name'], 'awg3-20260101T000000Z.tar.gz')

    def test_remote_error_is_reported(self):
        ssh = FakeSSH(command_code=1, command_out='')
        # Preserve the recorded code through the fake: rerun with err text.
        ssh.command_out = 'denied'
        result = BackupManager(ssh).list_backups('awg3')
        self.assertEqual(result['status'], 'error')


class CreateBackupTests(unittest.TestCase):
    def test_create_uses_run_sudo_script_with_extended_timeout(self):
        ssh = FakeSSH(script_out='/opt/amnezia/backups/awg3/awg3-20260101T000000Z.tar.gz\n')
        result = BackupManager(ssh).create_backup('awg3', 'amnezia-awg3')
        self.assertEqual(result['status'], 'success')
        self.assertEqual(len(ssh.scripts), 1)
        _, timeout = ssh.scripts[0]
        self.assertEqual(timeout, 180)

    def test_create_rejects_archive_outside_backup_root(self):
        ssh = FakeSSH(script_out='/tmp/awg3-evil.tar.gz\n')
        result = BackupManager(ssh).create_backup('awg3', 'amnezia-awg3')
        self.assertEqual(result['status'], 'error')


class DeleteBackupTests(unittest.TestCase):
    def test_invalid_filename_rejected_before_ssh(self):
        ssh = FakeSSH()
        result = BackupManager(ssh).delete_backup('awg3', '../../etc/passwd.tar.gz')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['message'], 'Invalid backup filename')
        self.assertEqual(ssh.commands, [])

    def test_delete_runs_test_and_rm_in_one_shell(self):
        ssh = FakeSSH()
        result = BackupManager(ssh).delete_backup('awg3', 'awg3-20260101.tar.gz')
        self.assertEqual(result['status'], 'success')
        self.assertEqual(len(ssh.commands), 1)
        cmd = ssh.commands[0]
        self.assertTrue(cmd.startswith('bash -c '), cmd)
        self.assertIn('test -f', cmd)
        self.assertIn('rm -f', cmd)

    def test_missing_backup_is_an_error(self):
        ssh = FakeSSH(command_code=1)
        result = BackupManager(ssh).delete_backup('awg3', 'awg3-20260101.tar.gz')
        self.assertEqual(result['status'], 'error')


class UploadBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='amnezia-test-upload-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _archive(self, protocol='awg3'):
        return _make_archive(os.path.join(self.tmp, 'upload.tar.gz'), protocol=protocol)

    def test_invalid_archive_rejected(self):
        bad = os.path.join(self.tmp, 'bad.tar.gz')
        with open(bad, 'wb') as fh:
            fh.write(b'not a tar.gz at all')
        result = BackupManager(FakeSSH()).upload_backup('awg3', 'bad.tar.gz', bad)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['message'], 'Invalid backup archive')

    def test_protocol_mismatch_rejected(self):
        path = self._archive(protocol='xray')
        result = BackupManager(FakeSSH()).upload_backup('awg3', 'xray-1.tar.gz', path)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['message'], 'Backup protocol mismatch')

    def test_success_puts_via_sftp_and_moves_in_one_shell(self):
        path = self._archive()
        # `test -f <dest>` answers 1 (absent); the final mv chain answers 0.
        ssh = FakeSSH(command_handler=lambda c: ('', '', 1 if c.startswith('test -f') else 0))
        result = BackupManager(ssh).upload_backup('awg3', 'custom.tar.gz', path)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['backup']['name'], 'custom.tar.gz')
        self.assertEqual(len(ssh.transfers), 1)
        op, local, remote, dst = ssh.transfers[0]
        self.assertEqual(op, 'put')
        self.assertTrue(os.path.exists(dst))
        final = ssh.commands[-1]
        self.assertTrue(final.startswith('bash -c '), final)
        self.assertIn('mv', final)
        self.assertIn('/opt/amnezia/backups/awg3/custom.tar.gz', final)

    def test_existing_destination_gets_a_fresh_stamp(self):
        path = self._archive()
        ssh = FakeSSH(command_code=0)  # test -f succeeds -> collision
        result = BackupManager(ssh).upload_backup('awg3', 'custom.tar.gz', path)
        self.assertEqual(result['status'], 'success')
        self.assertNotEqual(result['backup']['name'], 'custom.tar.gz')
        self.assertTrue(result['backup']['name'].startswith('awg3-'))
        self.assertTrue(result['backup']['name'].endswith('.tar.gz'))


class FetchRemoteArchiveTests(unittest.TestCase):
    def test_fetch_chain_runs_in_one_shell(self):
        seed = _make_archive(os.path.join(tempfile.mkdtemp(), 'seed.tar.gz'))
        ssh = FakeSSH()
        ssh.sftp_sources['_amnz_validate_awg2-2026.tar.gz'] = seed
        local, err = BackupManager(ssh)._fetch_remote_archive(
            '/opt/amnezia/backups/awg2/awg2-2026.tar.gz')
        self.assertIsNone(err)
        cmd = ssh.commands[0]
        self.assertTrue(cmd.startswith('sh -c '), cmd)
        self.assertIn('/opt/amnezia/backups/awg2/awg2-2026.tar.gz', cmd)
        self.assertIn('chmod 0644', cmd)
        self.assertTrue(os.path.exists(local))
        os.remove(local)

    def test_unsafe_remote_name_rejected(self):
        ssh = FakeSSH()
        local, err = BackupManager(ssh)._fetch_remote_archive('/opt/amnezia/backups/awg/x;id|.tar.gz')
        self.assertIsNone(local)
        self.assertEqual(err, 'Invalid backup filename')
        self.assertEqual(ssh.commands, [])

    def test_missing_remote_file_is_reported(self):
        ssh = FakeSSH(command_code=1)
        local, err = BackupManager(ssh)._fetch_remote_archive(
            '/opt/amnezia/backups/awg2/awg2-2026.tar.gz')
        self.assertIsNone(local)
        self.assertEqual(err, 'Backup not found')


class RestoreBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='amnezia-test-restore-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _ssh_with_archive(self, protocol='awg3'):
        archive = _make_archive(os.path.join(self.tmp, 'seed.tar.gz'), protocol=protocol)
        ssh = FakeSSH()
        ssh.sftp_sources['_amnz_validate_awg3-20260101.tar.gz'] = archive
        return ssh

    def test_invalid_filename_rejected_before_ssh(self):
        ssh = FakeSSH()
        result = BackupManager(ssh).restore_backup('awg3', 'amnezia-awg3', 'no-extension')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(ssh.commands, [])
        self.assertEqual(ssh.scripts, [])

    def test_missing_archive_is_an_error(self):
        ssh = FakeSSH(command_code=1)
        result = BackupManager(ssh).restore_backup('awg3', 'amnezia-awg3', 'awg3-20260101.tar.gz')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(ssh.scripts, [])

    def test_protocol_mismatch_rejected_after_inspect(self):
        ssh = self._ssh_with_archive(protocol='xray')
        result = BackupManager(ssh).restore_backup('awg3', 'amnezia-awg3', 'awg3-20260101.tar.gz')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['message'], 'Backup protocol mismatch')
        self.assertEqual(ssh.scripts, [])

    def test_success_fetches_inspects_and_runs_restore_script(self):
        ssh = self._ssh_with_archive()
        result = BackupManager(ssh).restore_backup('awg3', 'amnezia-awg3', 'awg3-20260101.tar.gz')
        self.assertEqual(result['status'], 'success')
        self.assertEqual(len(ssh.scripts), 1)
        script, timeout = ssh.scripts[0]
        self.assertEqual(timeout, 180)
        self.assertIn('amnezia-restore-', script)
        self.assertIn('restore_success', script)
        # Staged remote copy is removed after the fetch.
        self.assertTrue(any(c.startswith('rm -f') for c in ssh.commands))


if __name__ == '__main__':
    unittest.main()
