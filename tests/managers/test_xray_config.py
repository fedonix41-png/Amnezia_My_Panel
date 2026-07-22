"""Pure-structure tests for the Xray ``server.json`` builder.

These exercise ``build_initial_server_json`` directly — no SSH, no Docker, no
I/O. The function was extracted from ``install_protocol`` (which previously
built the dict inline) so the generated Reality + Stats + API structure can be
regression-tested without a live server.
"""

import os

os.environ.setdefault("AWP_DEV", "1")

from managers.xray_manager import build_initial_server_json, XrayManager


def _build(**overrides):
    """Build with sane defaults; let tests override individual fields."""
    kwargs = dict(
        port=443,
        site_name="yahoo.com",
        private_key="PRIVKEY",
        short_id="ab12cd34",
    )
    kwargs.update(overrides)
    return build_initial_server_json(**kwargs)


class TestVlessInbound:
    def test_has_vless_inbound_on_port(self):
        cfg = _build(port=8443)
        inbounds = cfg["inbounds"]
        assert inbounds[0]["protocol"] == "vless"
        assert inbounds[0]["port"] == 8443
        assert inbounds[0]["tag"] == "proxy"

    def test_port_is_int_coerced(self):
        # install_protocol historically coerced port via int(port); the
        # extracted function must preserve that so "443" doesn't leak in as
        # a string (Xray rejects non-numeric ports).
        cfg = _build(port="443")
        assert cfg["inbounds"][0]["port"] == 443
        assert isinstance(cfg["inbounds"][0]["port"], int)


class TestRealityStream:
    def test_reality_stream_settings(self):
        cfg = _build(site_name="example.com")
        vless = cfg["inbounds"][0]
        ss = vless["streamSettings"]
        assert ss["network"] == "tcp"
        assert ss["security"] == "reality"
        rs = ss["realitySettings"]
        assert rs["dest"] == "example.com:443"
        assert rs["serverNames"] == ["example.com"]
        assert rs["privateKey"] == "PRIVKEY"

    def test_short_id_appears_in_shortids_list(self):
        cfg = _build(short_id="deadbeef")
        rs = cfg["inbounds"][0]["streamSettings"]["realitySettings"]
        assert rs["shortIds"] == ["deadbeef"]


class TestApiInbound:
    def test_api_inbound_on_loopback(self):
        cfg = _build()
        api_inbound = cfg["inbounds"][1]
        assert api_inbound["listen"] == "127.0.0.1"
        assert api_inbound["port"] == 10085
        assert api_inbound["protocol"] == "dokodemo-door"
        assert api_inbound["tag"] == "api"


class TestRouting:
    def test_routing_directs_api_inbound_to_api_outbound(self):
        cfg = _build()
        rules = cfg["routing"]["rules"]
        assert len(rules) >= 1
        rule = rules[0]
        assert rule["inboundTag"] == ["api"]
        assert rule["outboundTag"] == "api"
        assert rule["type"] == "field"


class TestStatsAndPolicy:
    def test_stats_and_policy_present(self):
        cfg = _build()
        assert cfg["stats"] == {}
        level0 = cfg["policy"]["levels"]["0"]
        assert level0["statsUserUplink"] is True
        system = cfg["policy"]["system"]
        assert system["statsInboundUplink"] is True


class TestOutbounds:
    def test_outbounds_freedom_default(self):
        cfg = _build()
        assert cfg["outbounds"][0]["protocol"] == "freedom"


class TestPurity:
    def test_no_public_key_embedded_in_config(self):
        # The Reality server.json embeds only privateKey + shortIds. The
        # public key is written to its own key file by install_protocol, so
        # it must NOT appear in the generated config structure.
        cfg = _build()
        vless = cfg["inbounds"][0]
        rs = vless["streamSettings"]["realitySettings"]
        assert "publicKey" not in rs
        assert rs["privateKey"] == "PRIVKEY"

    def test_independent_calls_do_not_share_state(self):
        a = _build(port=1000)
        b = _build(port=2000)
        assert a["inbounds"][0]["port"] == 1000
        assert b["inbounds"][0]["port"] == 2000


class TestInstanceNaming:
    """The protocol suffix (``xray__2``) maps to numbered container/config
    directories so multiple Xray instances can coexist. These naming helpers
    are pure — no SSH — so they are tested directly with ssh_manager=None."""

    def _mgr(self, protocol):
        return XrayManager(None, protocol)

    def test_default_instance_has_unnumbered_name(self):
        m = self._mgr("xray")
        assert m.instance == 1
        assert m.container_name == "amnezia-xray"
        assert m.image_name == "amneziavpn/amnezia-xray"
        assert m._config_dir() == "/opt/amnezia/xray"
        assert m._config_path() == "/opt/amnezia/xray/server.json"

    def test_numbered_suffix_drives_numbered_paths(self):
        m = self._mgr("xray__2")
        assert m.instance == 2
        assert m.container_name == "amnezia-xray-2"
        assert m.image_name == "amneziavpn/amnezia-xray-2"
        assert m._config_dir() == "/opt/amnezia/xray-2"
        assert m._config_path() == "/opt/amnezia/xray-2/server.json"

    def test_invalid_suffix_falls_back_to_instance_one(self):
        # A non-numeric suffix must not crash — it collapses to instance 1
        # (unnumbered layout), matching the defensive design.
        m = self._mgr("xray__notanumber")
        assert m.instance == 1
        assert m.container_name == "amnezia-xray"

