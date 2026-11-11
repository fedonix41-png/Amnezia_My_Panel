"""Pure-structure tests for the Xray ``server.json`` builder.

These exercise ``build_initial_server_json`` directly — no SSH, no Docker, no
I/O. The function was extracted from ``install_protocol`` (which previously
built the dict inline) so the generated Reality + Stats + API structure can be
regression-tested without a live server.
"""

import json
import os

os.environ.setdefault("AWP_DEV", "1")

from managers.xray_manager import (
    XRAY_CORE_VERSION,
    XrayManager,
    build_initial_server_json,
)


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


class TestDockerfileContent:
    """The container image pins an Xray-core release via a build ARG so the
    bump (upstream v1.8.10, CVE-2026-98432) touches one constant only."""

    def _content(self, protocol="xray"):
        return XrayManager(None, protocol)._dockerfile_content()

    def test_uses_versioned_build_arg(self):
        content = self._content()
        assert f'ARG XRAY_RELEASE="{XRAY_CORE_VERSION}"' in content
        assert "${XRAY_RELEASE}" in content
        assert XRAY_CORE_VERSION in content

    def test_no_hardcoded_core_download_left(self):
        # The v26.3.27 hardcode must be gone — it is the CVE-2026-98432 line.
        content = self._content()
        assert "v26.3.27" not in content
        assert "Xray-linux-64.zip" in content

    def test_bbr_congestion_control(self):
        # Ported with upstream 98e52be: bbr instead of hybla.
        assert "tcp_congestion_control = bbr" in self._content()

    def test_numbered_instance_uses_numbered_config_dir(self):
        assert "/opt/amnezia/xray-2/server.json" in self._content("xray__2")


class _AduFakeSSH:
    """Captures the API payload uploaded to the container and scripts the
    ``xray api adu`` stdout."""

    def __init__(self, api_out="Added 1 user", api_code=0):
        self.uploaded = {}
        self.api_out = api_out
        self.api_code = api_code

    def upload_file_sudo(self, content, path):
        self.uploaded[path] = content

    def run_sudo_command(self, cmd, timeout=None):
        if "xray api adu" in cmd:
            return self.api_out, "", self.api_code
        return "", "", 0


class TestAduPayload:
    """Xray-core v26 builds a full inbound config from the adu payload and
    silently adds 0 users when port/listen are missing — the payload must
    carry them, and 'Added 0 user' must be treated as a failure."""

    def _config(self):
        return build_initial_server_json(
            port=8443, site_name="yahoo.com",
            private_key="PRIVKEY", short_id="ab12cd34",
        )

    def _payload(self, ssh):
        payloads = [v for v in ssh.uploaded.values()
                    if '"inbounds"' in v and '"clients"' in v]
        assert len(payloads) == 1
        return json.loads(payloads[0])

    def test_payload_carries_port_and_listen(self):
        ssh = _AduFakeSSH()
        manager = XrayManager(ssh)
        ok = manager._xray_api_add_user(self._config(), {"id": "u1"})
        assert ok is True
        inbound = self._payload(ssh)["inbounds"][0]
        assert inbound["port"] == 8443
        assert inbound["listen"] == "0.0.0.0"
        assert inbound["tag"] == "proxy"

    def test_payload_port_defaults_to_443_when_unknown(self):
        ssh = _AduFakeSSH()
        manager = XrayManager(ssh)
        config = {"inbounds": [{"protocol": "vless", "tag": "proxy",
                                "settings": {"clients": []}}]}
        assert manager._xray_api_add_user(config, {"id": "u1"}) is True
        assert self._payload(ssh)["inbounds"][0]["port"] == 443

    def test_added_zero_user_is_an_error(self):
        # exit code 0 with "Added 0 user" must NOT pass (silent no-op add).
        ssh = _AduFakeSSH(api_out="Added 0 user")
        manager = XrayManager(ssh)
        assert manager._xray_api_add_user(self._config(), {"id": "u1"}) is False

    def test_api_failure_is_an_error(self):
        ssh = _AduFakeSSH(api_out="connection refused", api_code=1)
        manager = XrayManager(ssh)
        assert manager._xray_api_add_user(self._config(), {"id": "u1"}) is False


class _ContainerStateSSH:
    """Exposes docker_container_state like the pooled upstream SSH manager."""

    def __init__(self, state):
        self.state = state
        self.fallback_called = False

    def docker_container_state(self, name):
        return self.state

    def run_sudo_command(self, cmd, timeout=None):
        self.fallback_called = True
        return "amnezia-xray", "", 0


class _PlainSSH:
    """Local ssh_manager has no docker_container_state — the compat path
    must fall back to the docker ps check."""

    def __init__(self):
        self.fallback_called = False

    def run_sudo_command(self, cmd, timeout=None):
        self.fallback_called = True
        return "amnezia-xray", "", 0


class TestCheckProtocolInstalled:
    """docker_container_state-совместимость: pooled SSH менеджеры отвечают
    без docker ps; всё остальное падает на legacy-проверку."""

    def test_state_provider_short_circuits(self):
        ssh = _ContainerStateSSH((True, "running"))
        manager = XrayManager(ssh)
        assert manager.check_protocol_installed() is True
        assert ssh.fallback_called is False

    def test_state_provider_can_report_absent(self):
        ssh = _ContainerStateSSH((False, "missing"))
        manager = XrayManager(ssh)
        assert manager.check_protocol_installed() is False
        assert ssh.fallback_called is False

    def test_none_state_falls_back_to_docker_ps(self):
        ssh = _ContainerStateSSH(None)
        manager = XrayManager(ssh)
        assert manager.check_protocol_installed() is True
        assert ssh.fallback_called is True

    def test_ssh_without_state_method_falls_back(self):
        ssh = _PlainSSH()
        manager = XrayManager(ssh)
        assert manager.check_protocol_installed() is True
        assert ssh.fallback_called is True


class _ConfigStubMixin:
    """Stubs the three remote reads get_client_config/rename_client need."""

    META = {"public_key": "PUBKEY", "short_id": "ab12cd34",
            "site_name": "yahoo.com", "port": 443}

    def _manager(self, table):
        manager = XrayManager(None)
        manager._get_clients_table = lambda: table
        manager._get_meta_json = lambda: dict(self.META)
        manager._get_server_json = lambda: None
        return manager


class TestPublicPort:
    """per-instance public address (upstream 959ec78, manager side): an
    explicit public_port beats the meta port; unset keeps meta/panel port."""

    def test_meta_port_used_by_default(self):
        manager = _ConfigStubMixin()._manager(
            [{"clientId": "u1", "userData": {"clientName": "vpn"}}])
        url = manager.get_client_config("xray", "u1", "10.0.0.1", 443)
        assert "u1@10.0.0.1:443?" in url

    def test_public_port_beats_meta_port(self):
        manager = _ConfigStubMixin()._manager(
            [{"clientId": "u1", "userData": {"clientName": "vpn"}}])
        url = manager.get_client_config("xray", "u1", "10.0.0.1", 443,
                                        public_port=8443)
        assert "u1@10.0.0.1:8443?" in url

    def test_missing_client_returns_none(self):
        manager = _ConfigStubMixin()._manager([])
        assert manager.get_client_config("xray", "u1", "10.0.0.1", 443,
                                         public_port=8443) is None


class TestRenameClient:
    def test_rename_updates_table_entry(self):
        manager = XrayManager(None)
        table = [{"clientId": "u1", "userData": {"clientName": "old"}}]
        saved = []
        manager._get_clients_table = lambda: table
        manager._save_clients_table = lambda t: saved.append(t)
        result = manager.rename_client("xray", "u1", "new")
        assert result == {"status": "success", "name": "new"}
        assert table[0]["userData"]["clientName"] == "new"
        assert saved == [table]

    def test_rename_missing_client_raises(self):
        manager = XrayManager(None)
        manager._get_clients_table = lambda: []
        manager._save_clients_table = lambda t: None
        try:
            manager.rename_client("xray", "nope", "new")
        except RuntimeError as e:
            assert "not found" in str(e)
        else:
            raise AssertionError("expected RuntimeError")

