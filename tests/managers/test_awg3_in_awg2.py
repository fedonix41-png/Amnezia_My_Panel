"""AWG 3.x detection inside an amnezia-awg2 container (upstream test_awg3_in_awg2).

The official Amnezia client installs AWG 3.x into the ``amnezia-awg2``
container, so the container name never tells the version apart — only the
config keys do. These tests pin ``AWGManager.get_server_status`` detection:

* HeaderProtectionKey present            -> ``protocol_version == '3.x'``
* RandomTrailers / DisableCookies added  -> ``protocol_version == '3.1'``
  (any non-empty value, including an explicit "off")
* plain AWG 2.0 config                   -> ``protocol_version == '2.0'``
  and ``header_protection`` False

Upstream also covers app.py naming/persistence (``protocol_display_name``
with header-protection flags, ``/api/servers/{id}/check`` display names) —
that lives in the upstream monolith and is intentionally not ported; the
local stage-1 port expresses the same via the ``awg3`` registry base.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("AWP_DEV", "1")

from managers.awg_manager import AWGManager

CONFIG_PATH = '/opt/amnezia/awg/awg0.conf'

SERVER_CONFIG = """[Interface]
PrivateKey = SERVERPRIVATEKEY
Address = 10.8.1.1/24
ListenPort = 55424
Jc = 3
Jmin = 10
Jmax = 30
S1 = 15
S2 = 18
H1 = 1020325451
H2 = 3288052141
H3 = 1770004689
H4 = 2528465083

[Peer]
PublicKey = PEER_A
PresharedKey = PSK_A
AllowedIPs = 10.8.1.2/32

[Peer]
PublicKey = PEER_B
PresharedKey = PSK_B
AllowedIPs = 10.8.1.3/32
"""

# What the official Amnezia client leaves in amnezia-awg2 when it installs
# AWG 3.x: the same container name, only the 3.x keys in the config.
AWG3_SERVER_CONFIG = SERVER_CONFIG.replace(
    'H4 = 2528465083\n',
    'H4 = 2528465083\nHeaderProtectionKey = HPKEY\nContentPaddingAddition = 16\n',
)


def status_for(config):
    manager = AWGManager(None)
    manager._get_server_config = lambda proto: config
    manager._get_clients_table = lambda proto: []
    with mock.patch.object(manager, 'check_protocol_installed', return_value=True), \
            mock.patch.object(manager, 'check_container_running', return_value=True):
        return manager.get_server_status('awg2')


class HeaderProtectionStatusTests(unittest.TestCase):
    def test_minor_version_requires_31_parameters(self):
        self.assertEqual(status_for(AWG3_SERVER_CONFIG)['protocol_version'], '3.x')
        self.assertEqual(status_for(SERVER_CONFIG)['protocol_version'], '2.0')
        for key in ('RandomTrailers', 'DisableCookies'):
            for value in ('on', 'off'):
                with self.subTest(key=key, value=value):
                    config = AWG3_SERVER_CONFIG.replace('[Peer]', f'{key} = {value}\n\n[Peer]', 1)
                    self.assertEqual(status_for(config)['protocol_version'], '3.1')

    def test_awg3_config_in_awg2_container_is_flagged(self):
        self.assertTrue(status_for(AWG3_SERVER_CONFIG)['header_protection'])

    def test_plain_awg2_config_is_not_flagged(self):
        self.assertFalse(status_for(SERVER_CONFIG)['header_protection'])

    def test_15_config_has_no_version_flag(self):
        # awg (1.5) base: neither marker — no protocol_version key at all.
        config_15 = SERVER_CONFIG.replace(
            'H4 = 2528465083\n',
            'H4 = 2528465083\nI1 = 1\nI2 = 2\nI3 = 3\nI4 = 4\nI5 = 5\nCPS = 10\n',
        )
        manager = AWGManager(None)
        manager._get_server_config = lambda proto: config_15
        manager._get_clients_table = lambda proto: []
        with mock.patch.object(manager, 'check_protocol_installed', return_value=True), \
                mock.patch.object(manager, 'check_container_running', return_value=True):
            status = manager.get_server_status('awg')
        self.assertNotIn('protocol_version', status)
        self.assertFalse(status['header_protection'])


if __name__ == '__main__':
    unittest.main()
