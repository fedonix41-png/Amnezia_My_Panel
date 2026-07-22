"""Basic validity check on AWG key/parameter generation."""

import os

os.environ.setdefault("AWP_DEV", "1")

from managers.awg_manager import generate_wg_keypair, generate_awg_params


class TestAwgKeyGeneration:
    def test_generate_wg_keypair(self):
        priv, pub = generate_wg_keypair()
        assert priv.endswith("=")
        assert pub.endswith("=")
        assert len(priv) > 30
        assert len(pub) > 30
        assert priv != pub

    def test_generate_awg_params_defaults(self):
        params = generate_awg_params()
        assert "junk_packet_count" in params
        assert "junk_packet_min_size" in params
        assert "init_packet_magic_header" in params
        assert int(params["junk_packet_count"]) > 0

    def test_generate_awg_params_are_randomized(self):
        p1 = generate_awg_params()
        p2 = generate_awg_params()
        all_same = all(p1.get(k) == p2.get(k) for k in p1)
        assert not all_same, "AWG params should be randomized across calls"
