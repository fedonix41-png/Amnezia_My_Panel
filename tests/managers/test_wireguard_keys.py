"""WireGuard key generation baseline tests."""

import os

os.environ.setdefault("AWP_DEV", "1")

from managers.wireguard_manager import generate_wg_keypair


class TestWireGuardKeys:
    def test_generate_keys(self):
        priv, pub = generate_wg_keypair()
        assert priv.endswith("=")
        assert pub.endswith("=")
        assert len(priv) > 30
        assert len(pub) > 30
        assert priv != pub
