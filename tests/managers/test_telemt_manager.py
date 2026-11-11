"""TelemtManager client lifecycle and FakeTLS fallback links (WP3.6).

Adapted from upstream tests/test_telemt_manager.py (v1.8.10): link building
now goes through _build_client_link, so every fallback link carries the
instance's FakeTLS padding (ee<secret><hex domain>) when tls_emulation is on.
"""

import unittest
from unittest.mock import patch

from managers.telemt_manager import TelemtManager


BASE_CONFIG = """### Telemt Based Config.toml

[general]
use_middle_proxy = true
log_level = "normal"

[general.modes]
classic = false
secure = false
tls = true

[general.links]
show = "*"
public_port = 443

[server]
port = 443
metrics_port = 9090

[server.api]
enabled = true
listen = "0.0.0.0:9091"

[[server.listeners]]
ip = "0.0.0.0"

[censorship]
tls_domain = "petrovich.ru"
mask = false
tls_emulation = true

[access.users]
# format: "username" = "32_hex_chars_secret"
hello = "00000000000000000000000000000000"
"""


class FakeSSH:
    def __init__(self, config=BASE_CONFIG):
        self.host = '203.0.113.10'
        self.config = config
        self.uploads = []
        self.api_commands = []
        self.sudo_commands = []

    def run_command(self, cmd, *args, **kwargs):
        return '', '', 0

    def run_sudo_command(self, cmd, *args, **kwargs):
        self.sudo_commands.append(cmd)
        if 'curl' in cmd:
            self.api_commands.append(cmd)
        if cmd.startswith('cat '):
            return self.config, '', 0
        return '', '', 0

    def upload_file_sudo(self, content, remote_path):
        self.uploads.append((remote_path, content))
        if remote_path.endswith('config.toml'):
            self.config = content


class TelemtManagerClientTest(unittest.TestCase):
    def make_manager(self, config=BASE_CONFIG):
        ssh = FakeSSH(config)
        manager = TelemtManager(ssh)
        # API is unreachable in tests; add_client has a tg:// fallback for links
        manager._api_request = lambda *a, **k: None
        return ssh, manager

    def test_add_client_does_not_raise_name_error(self):
        ssh, manager = self.make_manager()
        result = manager.add_client('telemt', 'My Client', host='vpn.example.test', port='443')

        self.assertEqual(result['client_id'], 'My_Client')
        self.assertTrue(result['config'].startswith('tg://proxy?'))
        self.assertIn('secret=', result['config'])

        uploaded = [c for _, c in ssh.uploads]
        self.assertTrue(uploaded, 'config must be uploaded to the host')
        self.assertTrue(any('My_Client = "' in c for c in uploaded),
                        'uploaded config must contain the new user')

    def test_add_client_with_quota_and_limits(self):
        ssh, manager = self.make_manager()
        ad_tag = 'a' * 32
        manager.add_client(
            'telemt', 'bob', host='vpn.example.test', port='443',
            telemt_quota='1073741824', telemt_max_ips='3',
            telemt_expiry='2026-12-31T23:59:59Z', user_ad_tag=ad_tag,
            max_tcp_conns='100',
        )

        uploaded = '\n'.join(c for _, c in ssh.uploads)
        self.assertIn('[access.user_data_quota]', uploaded)
        self.assertIn('bob = 1073741824', uploaded)
        self.assertIn('[access.user_max_unique_ips]', uploaded)
        self.assertIn('bob = 3', uploaded)
        self.assertIn('[access.user_expirations]', uploaded)
        self.assertIn('bob = "2026-12-31T23:59:59Z"', uploaded)
        self.assertIn('[access.user_ad_tags]', uploaded)
        self.assertIn('bob = "%s"' % ad_tag, uploaded)
        self.assertIn('[access.user_max_tcp_conns]', uploaded)
        self.assertIn('bob = 100', uploaded)

    def test_edit_client_does_not_raise_name_error(self):
        ssh, manager = self.make_manager()
        result = manager.edit_client('telemt', 'hello', {'telemt_quota': '2048'})

        self.assertEqual(result['status'], 'success')
        uploaded = '\n'.join(c for _, c in ssh.uploads)
        self.assertIn('hello = 2048', uploaded)

    def test_add_client_generates_unique_names(self):
        ssh, manager = self.make_manager()
        first = manager.add_client('telemt', 'hello', host='h', port='443')
        second = manager.add_client('telemt', 'hello', host='h', port='443')

        self.assertEqual(first['client_id'], 'hello_1')
        self.assertEqual(second['client_id'], 'hello_2')

    def test_add_client_faketls_fallback_keeps_raw_secret_in_config(self):
        ssh, manager = self.make_manager()
        secret = 'ab' * 16
        result = manager.add_client(
            'telemt', 'alice', host='vpn.example.test', port='8443', secret=secret)
        expected = ('tg://proxy?server=vpn.example.test&port=8443&secret=ee'
                    + secret + 'petrovich.ru'.encode('utf-8').hex())
        self.assertEqual(result['config'], expected)
        self.assertEqual(result['vpn_link'], expected)
        self.assertIn(f'alice = "{secret}"', ssh.config)

    def test_add_client_last_resort_fallback_uses_faketls(self):
        _, manager = self.make_manager()
        secret = 'ab' * 16
        with patch.object(manager, 'get_client_config', return_value='Not found'):
            result = manager.add_client('telemt', 'alice', host='h', port='443', secret=secret)
        self.assertEqual(result['config'], 'tg://proxy?server=h&port=443&secret=ee'
                         + secret + 'petrovich.ru'.encode('utf-8').hex())

    def test_get_client_config_fallback_uses_public_port(self):
        _, manager = self.make_manager()
        link = manager.get_client_config(
            'telemt', 'hello', host='vpn.example.test', port='8443', public_port='443')
        self.assertEqual(link, 'tg://proxy?server=vpn.example.test&port=443&secret=ee'
                         + '0' * 32 + 'petrovich.ru'.encode('utf-8').hex())

    def test_get_client_config_without_faketls_keeps_raw_secret(self):
        _, manager = self.make_manager(BASE_CONFIG.replace('tls_emulation = true',
                                                           'tls_emulation = false'))
        self.assertEqual(manager.get_client_config('telemt', 'hello', 'h', '443'),
                         'tg://proxy?server=h&port=443&secret=' + '0' * 32)

    def test_get_client_config_api_without_links_uses_faketls_fallback(self):
        _, manager = self.make_manager()
        manager._api_request = lambda *a, **k: {'ok': True, 'data': [] if a[1] == '/v1/users' else {}}
        self.assertEqual(manager.get_client_config('telemt', 'hello', 'h', '443'),
                         'tg://proxy?server=h&port=443&secret=ee'
                         + '0' * 32 + 'petrovich.ru'.encode('utf-8').hex())

    def test_get_client_config_unknown_client_is_not_found(self):
        _, manager = self.make_manager()
        self.assertEqual(manager.get_client_config('telemt', 'missing', 'h', '443'), 'Not found')

    def test_get_client_config_missing_faketls_domain_reports_error(self):
        for domain_line in ('', 'tls_domain = ""'):
            with self.subTest(domain_line=domain_line):
                _, manager = self.make_manager(BASE_CONFIG.replace(
                    'tls_domain = "petrovich.ru"', domain_line))
                with self.assertRaisesRegex(RuntimeError, 'tls_domain is missing'):
                    manager.get_client_config('telemt', 'hello', 'h', '443')

    def test_get_client_config_prefers_api_link(self):
        _, manager = self.make_manager()
        api_link = 'tg://proxy?server=api.example.test&port=443&secret=ee' + 'ab' * 16
        manager._api_request = lambda *a, **k: {
            'ok': True, 'data': {'links': {'tls': [api_link], 'classic': ['other']}}}
        with patch.object(manager, '_get_server_config', side_effect=AssertionError):
            self.assertEqual(manager.get_client_config('telemt', 'hello', 'h', '8443'), api_link)

    def test_fallback_uses_selected_instance_domain(self):
        ssh = FakeSSH(BASE_CONFIG.replace('petrovich.ru', 'example.com'))
        manager = TelemtManager(ssh, protocol='telemt__2')
        manager._api_request = lambda *a, **k: None
        link = manager.get_client_config('telemt__2', 'hello', 'h', '443')
        self.assertEqual(link, 'tg://proxy?server=h&port=443&secret=ee'
                         + '0' * 32 + '6578616d706c652e636f6d')


class ApiRequestQuotingTests(unittest.TestCase):
    """_api_request must build one shell command with every arg quoted."""

    def test_payload_and_url_are_quoted_and_separated_by_double_dash(self):
        ssh = FakeSSH()
        manager = TelemtManager(ssh)
        manager._api_request("POST", "/v1/users", data={"username": "a b"})
        self.assertEqual(len(ssh.api_commands), 1)
        cmd = ssh.api_commands[0]
        self.assertIn('--globoff', cmd)
        self.assertIn("-- http://127.0.0.1:9091/v1/users", cmd)
        # JSON payload with a space survives shell interpretation.
        self.assertIn("-d " + __import__('shlex').quote('{"username": "a b"}'), cmd)

    def test_get_request_has_no_payload(self):
        ssh = FakeSSH()
        manager = TelemtManager(ssh)
        manager._api_request("GET", "/v1/users")
        cmd = ssh.api_commands[0]
        self.assertNotIn('Content-Type', cmd)
        self.assertIn("-- http://127.0.0.1:9091/v1/users", cmd)


class RenameClientTests(unittest.TestCase):
    def _manager_with_api(self, config=BASE_CONFIG):
        ssh = FakeSSH(config)
        manager = TelemtManager(ssh)
        calls = []
        manager._api_request = lambda method, path, data=None: calls.append((method, path, data)) or {'ok': True}
        return ssh, manager, calls

    def test_rename_rewrites_all_sections_and_recreates_runtime_user(self):
        config = BASE_CONFIG + '[access.user_data_quota]\nhello = 1024\n'
        ssh, manager, calls = self._manager_with_api(config)
        result = manager.rename_client('telemt', 'hello', 'bob')

        self.assertEqual(result['client_id'], 'bob')
        users = manager._parse_users_from_config(ssh.config)
        self.assertIn('bob', users)
        self.assertNotIn('hello', users)
        self.assertIn('bob = 1024', ssh.config)
        methods = [(m, p) for m, p, _ in calls]
        self.assertIn(('DELETE', '/v1/users/hello'), methods)
        self.assertIn(('POST', '/v1/users'), methods)
        post = next(c for c in calls if c[0] == 'POST')
        self.assertEqual(post[2]['username'], 'bob')
        self.assertEqual(post[2]['secret'], '0' * 32)
        self.assertTrue(any('docker kill -s HUP' in c for c in ssh.sudo_commands))

    def test_rename_preserves_disabled_marker(self):
        config = BASE_CONFIG.replace('hello = "', '# hello = "')
        ssh, manager, calls = self._manager_with_api(config)
        result = manager.rename_client('telemt', 'hello', 'bob')

        self.assertEqual(result['client_id'], 'bob')
        self.assertIn('# bob = "00000000000000000000000000000000"', ssh.config)
        # A disabled user is not re-created in the runtime API.
        methods = [(m, p) for m, p, _ in calls]
        self.assertNotIn(('POST', '/v1/users'), methods)

    def test_rename_unknown_client_raises(self):
        _, manager, _ = self._manager_with_api()
        with self.assertRaisesRegex(RuntimeError, 'Client not found'):
            manager.rename_client('telemt', 'missing', 'bob')

    def test_rename_to_existing_name_raises(self):
        config = BASE_CONFIG + 'taken = "' + '1' * 32 + '"\n'
        _, manager, _ = self._manager_with_api(config)
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            manager.rename_client('telemt', 'hello', 'taken')


if __name__ == '__main__':
    unittest.main()
