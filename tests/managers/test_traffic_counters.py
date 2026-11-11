"""Strict parsing and transport assembly for ``managers/traffic_counters``.

Accounting reads must never trust display/cache fallbacks, so the snapshot
frame is marker-delimited and validated field by field; anything malformed
raises ValueError without leaking remote output into the exception. The
bracketing docker-inspect epochs make the sample restart-safe.

The upstream sibling (test_shared_traffic_accounting) also covers
user_state/apply_traffic_samples — that module does not exist in this fork
(accounting lives in app.py), so only the manager-level contract is tested
here.
"""

import base64
import hashlib
import os
import unittest

os.environ.setdefault("AWP_DEV", "1")

from managers.traffic_counters import (
    counter,
    parse_transfer_snapshot,
    read_transfer_snapshot,
)

KEYS = [base64.b64encode(bytes([i]) * 32).decode() for i in range(1, 4)]
HEADER = ('COUNTERS_V1\n'
          + 'a' * 64 + ' 2026-10-01T00:00:00Z 123\n'
          + '12345678-1234-1234-1234-123456789abc\n'
          + '10\n'
          + 'no-socket')


def frame(rows, header=HEADER):
    return header + '\nTRANSFER\n' + '\n'.join(rows) + '\nEND'


ROWS = [f'{k}\t40\t0' for k in KEYS]


class _SnapshotSSH:
    """Returns one scripted snapshot frame for any sudo command."""

    def __init__(self, raw=None, code=0):
        self.raw = raw if raw is not None else frame(ROWS)
        self.code = code
        self.commands = []

    def run_sudo_command(self, command, timeout=60):
        self.commands.append(command)
        return self.raw, 'synthetic unavailable' if self.code else '', self.code


class CounterTests(unittest.TestCase):
    def test_int_only_no_bool_no_str(self):
        self.assertTrue(counter(0))
        self.assertTrue(counter(42))
        self.assertFalse(counter(-1))
        self.assertFalse(counter(True))   # bool is an int subclass — reject
        self.assertFalse(counter('40'))
        self.assertFalse(counter(4.0))


class ParseTests(unittest.TestCase):
    def test_valid_frame_parses_peers(self):
        snap = parse_transfer_snapshot(frame(ROWS))
        self.assertEqual(set(snap['peers']), set(KEYS))
        for k in KEYS:
            self.assertEqual(snap['peers'][k], {'rx_bytes': 40, 'tx_bytes': 0})
        self.assertEqual(len(snap['epoch']), 64)

    def test_epoch_is_header_hash(self):
        raw = frame(ROWS)
        header = raw.split('\nTRANSFER\n', 1)[0]
        self.assertEqual(parse_transfer_snapshot(raw)['epoch'],
                         hashlib.sha256(header.encode()).hexdigest())

    def test_socket_identity_forms_accepted(self):
        for sockets in ('no-socket', '12:3456', '12:3456:78:9012'):
            header = HEADER.replace('no-socket', sockets)
            with self.subTest(sockets=sockets):
                self.assertEqual(len(parse_transfer_snapshot(frame(ROWS, header))['peers']), 3)

    def test_garbage_rejected(self):
        bad = [
            '', 'COUNTERS_V1', 'OTHER\n' + HEADER.split('\n', 1)[1] + '\nTRANSFER\n' + '\n'.join(ROWS) + '\nEND',
            frame(ROWS)[:-4],                      # END marker cut off
            frame(ROWS) + '\n',                     # trailing junk after END
            frame(['short-key\t40\t0']),            # peer key not 32 bytes b64
            frame(['%%%\t40\t0']),
            frame([f'{KEYS[0]}\tforty\t0']),
            frame([f'{KEYS[0]}\t40\t0\textra']),
            frame([f'{KEYS[0]}\t40\t0', f'{KEYS[0]}\t41\t0']),  # duplicate peer
            frame(ROWS, HEADER.replace('10\nno-socket', 'ten\nno-socket')),
            frame(ROWS, HEADER.replace('no-socket', 'bogus socket!')),
        ]
        for raw in bad:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ValueError):
                    parse_transfer_snapshot(raw)

    def test_non_string_input_rejected(self):
        for raw in (None, 42, b'bytes-not-str', {'a': 1}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_transfer_snapshot(raw)


class ReadSnapshotTests(unittest.TestCase):
    def test_happy_path_returns_parsed_peers(self):
        ssh = _SnapshotSSH()
        snap = read_transfer_snapshot(ssh, 'amnezia-awg', 'awg', 'awg0')
        self.assertEqual(len(snap['peers']), 3)
        self.assertIn('docker inspect', ssh.commands[0])
        self.assertIn('show awg0 transfer', ssh.commands[0])

    def test_remote_failure_raises_runtime_error(self):
        ssh = _SnapshotSSH(code=1)
        with self.assertRaises(RuntimeError):
            read_transfer_snapshot(ssh, 'amnezia-awg', 'awg', 'awg0')

    def test_malformed_frame_raises_value_error(self):
        ssh = _SnapshotSSH(raw='not a snapshot')
        with self.assertRaises(ValueError):
            read_transfer_snapshot(ssh, 'amnezia-awg', 'awg', 'awg0')

    def test_endpoint_injection_rejected(self):
        ssh = _SnapshotSSH()
        for bad in ('amnezia-awg; id', 'awg rm -rf', 'awg0 --help',
                    '', 'a b', '../etc'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    read_transfer_snapshot(ssh, bad, 'awg', 'awg0')
        # container/binary/interface are validated as a triple
        for container, binary, interface in (
                ('amnezia-awg', 'bad binary', 'awg0'),
                ('amnezia-awg', 'awg', 'bad iface;id')):
            with self.subTest(binary=binary, interface=interface):
                with self.assertRaises(ValueError):
                    read_transfer_snapshot(ssh, container, binary, interface)


if __name__ == '__main__':
    unittest.main()
