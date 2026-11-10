"""Unit tests for the AmneziaVPN native JSON config builder.

Exercises the encoding utilities (qt_compress, base64url), config parsers
(WireGuard/AWG .conf, vless:// URL), JSON builders, and the high-level
generate_qr_content / generate_vpn_link functions. No SSH, no I/O.
"""

import base64
import json
import struct
import zlib

import pytest

from managers.amnezia_config import (
    qt_compress,
    base64url_encode,
    _parse_wg_config,
    _parse_vless_url,
    _detect_awg_version,
    _build_wg_json,
    _build_xray_json,
    build_amnezia_config,
    generate_qr_content,
    generate_vpn_link,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

AWG_V2_CONF = """[Interface]
Address = 10.8.0.2/32
DNS = 1.1.1.1, 1.0.0.1
PrivateKey = cLiEnTpRiVkEy=
MTU = 1280
Jc = 3
Jmin = 50
Jmax = 1000
S1 = 0
S2 = 0
S3 = 0
S4 = 0
H1 = 1
H2 = 2
H3 = 3
H4 = 4

[Peer]
PublicKey = sErVeRpUbKeY=
PresharedKey = pSk=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.com:55424
PersistentKeepalive = 25
"""

AWG_PLAIN_CONF = """[Interface]
Address = 10.8.0.3/32
PrivateKey = cLiEnTpRiVkEy=
MTU = 1280
Jc = 3
Jmin = 50
Jmax = 1000
S1 = 0
S2 = 0
H1 = 1
H2 = 2
H3 = 3
H4 = 4

[Peer]
PublicKey = sErVeRpUbKeY=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.com:55424
"""

WG_PLAIN_CONF = """[Interface]
Address = 10.8.0.4/32
PrivateKey = wGpRiV=
MTU = 1280

[Peer]
PublicKey = wGsErV=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.com:51820
"""

VLESS_URL = (
    "vless://aabb-ccdd-eeff@example.com:443"
    "?type=tcp&security=reality&pbk=MYKEY&sni=yahoo.com"
    "&fp=chrome&sid=dead&flow=xtls-rprx-vision#MyVPN"
)


# ---------------------------------------------------------------------------
# qt_compress / base64url
# ---------------------------------------------------------------------------

class TestQtCompress:
    def test_produces_valid_qcompress_format(self):
        data = b'{"test": true}'
        compressed = qt_compress(data)
        length = struct.unpack(">I", compressed[:4])[0]
        assert length == len(data)
        assert zlib.decompress(compressed[4:]) == data

    def test_roundtrip_with_qt_format(self):
        """Simulate qUncompress: read 4-byte length, then zlib decompress."""
        data = b'{"containers": []}'
        compressed = qt_compress(data)
        _length = struct.unpack(">I", compressed[:4])[0]
        decompressed = zlib.decompress(compressed[4:])
        assert decompressed == data


class TestBase64UrlEncode:
    def test_no_padding(self):
        encoded = base64url_encode(b"test data")
        assert "=" not in encoded

    def test_url_safe_alphabet(self):
        encoded = base64url_encode(b"\xff\xfe\xfd")
        assert "+" not in encoded
        assert "/" not in encoded


# ---------------------------------------------------------------------------
# Config parsers
# ---------------------------------------------------------------------------

class TestParseWgConfig:
    def test_parses_awg_config(self):
        result = _parse_wg_config(AWG_V2_CONF)
        assert result is not None
        assert result["PrivateKey"] == "cLiEnTpRiVkEy="
        assert result["Address"] == "10.8.0.2/32"
        assert result["PublicKey"] == "sErVeRpUbKeY="
        assert result["Jc"] == "3"
        assert result["S3"] == "0"

    def test_returns_none_for_missing_required(self):
        result = _parse_wg_config("[Interface]\nAddress = 10.0.0.1")
        assert result is None

    def test_skips_section_headers(self):
        result = _parse_wg_config(AWG_V2_CONF)
        assert "[Interface]" not in result
        assert "[Peer]" not in result


class TestParseVlessUrl:
    def test_parses_all_fields(self):
        result = _parse_vless_url(VLESS_URL)
        assert result["uuid"] == "aabb-ccdd-eeff"
        assert result["host"] == "example.com"
        assert result["port"] == "443"
        assert result["pbk"] == "MYKEY"
        assert result["sni"] == "yahoo.com"
        assert result["sid"] == "dead"

    def test_returns_none_for_non_vless(self):
        assert _parse_vless_url("https://example.com") is None

    def test_defaults_flow(self):
        url = "vless://uuid@host.com:443?pbk=k"
        result = _parse_vless_url(url)
        assert result["flow"] == "xtls-rprx-vision"


# ---------------------------------------------------------------------------
# JSON builders
# ---------------------------------------------------------------------------

class TestBuildWgJson:
    def test_awg_config_with_s3_s4_detected_as_v2(self):
        cfg_map = _parse_wg_config(AWG_V2_CONF)
        result = _build_wg_json(cfg_map, AWG_V2_CONF)
        assert result["containers"][0]["container"] == "amnezia-awg"
        assert result["containers"][0]["awg"]["protocol_version"] == "2"

    def test_awg_config_without_s3_s4_detected_as_plain_awg(self):
        cfg_map = _parse_wg_config(AWG_PLAIN_CONF)
        result = _build_wg_json(cfg_map, AWG_PLAIN_CONF)
        assert result["containers"][0]["container"] == "amnezia-awg"
        assert "protocol_version" not in result["containers"][0]["awg"]

    def test_plain_wireguard_detected(self):
        cfg_map = _parse_wg_config(WG_PLAIN_CONF)
        result = _build_wg_json(cfg_map, WG_PLAIN_CONF)
        assert result["containers"][0]["container"] == "amnezia-wireguard"
        assert "wireguard" in result["containers"][0]

    def test_last_config_contains_raw_config_text(self):
        cfg_map = _parse_wg_config(AWG_V2_CONF)
        result = _build_wg_json(cfg_map, AWG_V2_CONF)
        last_config = json.loads(result["containers"][0]["awg"]["last_config"])
        assert last_config["config"] == AWG_V2_CONF.strip()

    def test_required_fields_present(self):
        cfg_map = _parse_wg_config(AWG_V2_CONF)
        result = _build_wg_json(cfg_map, AWG_V2_CONF)
        last_config = json.loads(result["containers"][0]["awg"]["last_config"])
        assert last_config["hostName"] == "vpn.example.com"
        assert last_config["port"] == 55424
        assert last_config["client_priv_key"] == "cLiEnTpRiVkEy="
        assert last_config["server_pub_key"] == "sErVeRpUbKeY="

    def test_dns_extraction(self):
        cfg_map = _parse_wg_config(AWG_V2_CONF)
        result = _build_wg_json(cfg_map, AWG_V2_CONF)
        assert result["dns1"] == "1.1.1.1"
        assert result["dns2"] == "1.0.0.1"

    def test_allowed_ips_parsed_as_list(self):
        cfg_map = _parse_wg_config(AWG_V2_CONF)
        result = _build_wg_json(cfg_map, AWG_V2_CONF)
        last_config = json.loads(result["containers"][0]["awg"]["last_config"])
        assert "0.0.0.0/0" in last_config["allowed_ips"]
        assert "::/0" in last_config["allowed_ips"]

    def test_port_string_type(self):
        cfg_map = _parse_wg_config(AWG_V2_CONF)
        result = _build_wg_json(cfg_map, AWG_V2_CONF)
        assert result["containers"][0]["awg"]["port"] == "55424"

    def test_native_container_structure(self):
        cfg_map = _parse_wg_config(AWG_V2_CONF)
        result = _build_wg_json(cfg_map, AWG_V2_CONF)
        awg_sec = result["containers"][0]["awg"]
        # isThirdPartyConfig must NOT be set for native containers
        assert "isThirdPartyConfig" not in awg_sec
        # AWG obfuscation parameters must be present at top level of awg section
        assert awg_sec["Jc"] == "3"
        assert awg_sec["H1"] == "1"


class TestBuildXrayJson:
    def test_vless_url_parsed_correctly(self):
        parsed = _parse_vless_url(VLESS_URL)
        result = _build_xray_json(parsed)
        xray_cfg = json.loads(result["containers"][0]["xray"]["last_config"])
        vnext = xray_cfg["outbounds"][0]["settings"]["vnext"][0]
        assert vnext["address"] == "example.com"
        assert vnext["port"] == 443
        assert vnext["users"][0]["id"] == "aabb-ccdd-eeff"

    def test_container_is_amnezia_xray(self):
        parsed = _parse_vless_url(VLESS_URL)
        result = _build_xray_json(parsed)
        assert result["containers"][0]["container"] == "amnezia-xray"

    def test_reality_settings(self):
        parsed = _parse_vless_url(VLESS_URL)
        result = _build_xray_json(parsed)
        xray_cfg = json.loads(result["containers"][0]["xray"]["last_config"])
        rs = xray_cfg["outbounds"][0]["streamSettings"]["realitySettings"]
        assert rs["publicKey"] == "MYKEY"
        assert rs["serverName"] == "yahoo.com"
        assert rs["shortId"] == "dead"
        assert rs["fingerprint"] == "chrome"

    def test_hostName_at_top_level(self):
        parsed = _parse_vless_url(VLESS_URL)
        result = _build_xray_json(parsed)
        assert result["hostName"] == "example.com"


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

class TestBuildAmneziaConfig:
    def test_auto_detects_awg(self):
        result = build_amnezia_config(AWG_V2_CONF, "awg")
        assert result is not None
        assert result["containers"][0]["container"] == "amnezia-awg"

    def test_auto_detects_vless(self):
        result = build_amnezia_config(VLESS_URL, "xray")
        assert result is not None
        assert result["containers"][0]["container"] == "amnezia-xray"

    def test_returns_none_for_unknown(self):
        assert build_amnezia_config("some random text", "telemt") is None

    def test_returns_none_for_empty(self):
        assert build_amnezia_config("", "awg") is None


class TestGenerateQrContent:
    def test_has_no_vpn_prefix_but_magic(self):
        qr = generate_qr_content(AWG_V2_CONF, "awg")
        assert not qr.startswith("vpn://")
        # Base64url of 0x07C00100 starts with B8ABAA
        assert qr.startswith("B8ABAA")

    def test_returns_base64url_compressed_json(self):
        qr = generate_qr_content(AWG_V2_CONF, "awg")
        padded = qr + "=" * (-len(qr) % 4)
        raw = base64.urlsafe_b64decode(padded)
        import struct, zlib
        version, compressed_len, uncompressed_len = struct.unpack(">III", raw[:12])
        assert version == 0x07C00100
        # compressed_len includes the 4 bytes of uncompressed_len in Qt QByteArray format
        assert compressed_len == len(raw) - 12 + 4
        data = zlib.decompress(raw[12:])
        assert len(data) == uncompressed_len
        js = json.loads(data)
        assert "containers" in js

    def test_xray_qr_decodable(self):
        qr = generate_qr_content(VLESS_URL, "xray")
        padded = qr + "=" * (-len(qr) % 4)
        raw = base64.urlsafe_b64decode(padded)
        import struct, zlib
        v = struct.unpack(">I", raw[:4])[0]
        assert v == 0x07C00100
        data = zlib.decompress(raw[12:])
        js = json.loads(data)
        assert js["containers"][0]["container"] == "amnezia-xray"


class TestGenerateVpnLink:
    def test_returns_empty_string_for_invalid(self):
        assert generate_vpn_link("invalid", "telemt") == ""

    def test_has_vpn_prefix(self):
        link = generate_vpn_link(AWG_V2_CONF, "awg")
        assert link.startswith("vpn://")

    def test_different_from_qr_content(self):
        """QR content and vpn:// link are different."""
        qr = generate_qr_content(AWG_V2_CONF, "awg")
        link = generate_vpn_link(AWG_V2_CONF, "awg")
        assert link != qr
        
        # Link has vpn:// but QR does not
        assert link.startswith("vpn://")
        assert not qr.startswith("vpn://")
        
        # Link starts with uncompressed_len (00 00 0x 0x), QR starts with B8AB (07 C0 01 00)
        link_raw = base64.urlsafe_b64decode(link.replace("vpn://", "") + "==")
        qr_raw = base64.urlsafe_b64decode(qr + "==")
        import struct
        assert struct.unpack(">I", link_raw[:4])[0] > 100
        assert struct.unpack(">I", qr_raw[:4])[0] == 0x07C00100

class TestIntegration:
    def test_awg_qr_decodable_by_amnezia_client_path(self):
        qr = generate_qr_content(AWG_V2_CONF, "awg")
        padded = qr + "=" * (-len(qr) % 4)
        raw = base64.urlsafe_b64decode(padded)
        import struct, zlib
        v = struct.unpack(">I", raw[:4])[0]
        assert v == 0x07C00100
        
        compressed_len = struct.unpack(">I", raw[4:8])[0]
        uncompressed_len = struct.unpack(">I", raw[8:12])[0]
        assert compressed_len == len(raw) - 12 + 4
        
        decompressed = zlib.decompress(raw[12:])
        assert len(decompressed) == uncompressed_len
        js = json.loads(decompressed)
        assert js["defaultContainer"] == "amnezia-awg"

    def test_wireguard_qr_decodable(self):
        qr = generate_qr_content(WG_PLAIN_CONF, "wireguard")
        padded = qr + "=" * (-len(qr) % 4)
        raw = base64.urlsafe_b64decode(padded)
        import struct, zlib
        v = struct.unpack(">I", raw[:4])[0]
        assert v == 0x07C00100
        
        compressed_len = struct.unpack(">I", raw[4:8])[0]
        uncompressed_len = struct.unpack(">I", raw[8:12])[0]
        
        decompressed = zlib.decompress(raw[12:])
        js = json.loads(decompressed)
        assert js["defaultContainer"] == "amnezia-wireguard"


# ---------------------------------------------------------------------------
# AWG 3.x / 3.1 (ported from upstream v1.8.10 AWGManager.get_client_config)
# ---------------------------------------------------------------------------

AWG_V3_CONF = """[Interface]
Address = 10.8.0.2/32
DNS = 1.1.1.1, 1.0.0.1
PrivateKey = cLiEnTpRiVkEy=
MTU = 1376
Jc = 3
Jmin = 50
Jmax = 1000
S1 = 15
S2 = 15
S3 = 15
S4 = 15
H1 = 1
H2 = 2
H3 = 3
H4 = 4
I1 = 115792089237316195423570985008687907853269984665640564039457584007913129639935
I2 = 115792089237316195423570985008687907853269984665640564039457584007913129639935
I3 = 115792089237316195423570985008687907853269984665640564039457584007913129639935
I4 = 115792089237316195423570985008687907853269984665640564039457584007913129639935
I5 = 115792089237316195423570985008687907853269984665640564039457584007913129639935
CPS = signature
HeaderProtectionKey = hPk=
ContentPaddingAddition = 24
RekeyAfterTime = 120
RekeyTimeout = 15
RejectAfterTime = 180
KeepaliveTimeout = 10
MaxHandshakeAttempts = 8

[Peer]
PublicKey = sErVeRpUbKeY=
PresharedKey = pSk=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.com:55424
PersistentKeepalive = 25
"""

AWG3_PARAM_KEYS = (
    "HeaderProtectionKey", "ContentPaddingAddition",
    "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime",
    "KeepaliveTimeout", "MaxHandshakeAttempts",
    "RandomTrailers", "DisableCookies",
)


class TestDetectAwg3Version:
    def test_header_protection_key_means_3x(self):
        config_map = _parse_wg_config(AWG_V3_CONF)
        assert _detect_awg_version(config_map) == "3.x"

    def test_random_trailers_means_31(self):
        config_map = _parse_wg_config(AWG_V3_CONF + "RandomTrailers = 4\n")
        assert _detect_awg_version(config_map) == "3.1"

    def test_disable_cookies_off_still_means_31(self):
        # Explicit "off" also identifies the 3.1 config format (upstream
        # get_server_status checks for any non-empty value).
        config_map = _parse_wg_config(AWG_V3_CONF + "DisableCookies = off\n")
        assert _detect_awg_version(config_map) == "3.1"

    def test_v2_conf_still_detects_2(self):
        config_map = _parse_wg_config(AWG_V2_CONF)
        assert _detect_awg_version(config_map) == "2"

    def test_plain_awg_without_version_markers(self):
        config_map = _parse_wg_config(AWG_PLAIN_CONF)
        assert _detect_awg_version(config_map) is None


class TestBuildAwg3Json:
    def _proto_section(self, conf=AWG_V3_CONF, hint="awg3"):
        result = build_amnezia_config(conf, hint)
        assert result is not None
        return result["containers"][0]

    def test_container_amnezia_awg3_with_hint(self):
        entry = self._proto_section()
        assert entry["container"] == "amnezia-awg3"
        assert "awg3" in entry
        assert entry["awg3"]["protocol_version"] == "3.x"

    def test_all_awg3_param_fields_in_proto_section(self):
        proto = self._proto_section()["awg3"]
        for key in AWG3_PARAM_KEYS:
            assert key in proto, f"missing {key} in proto section"
        assert proto["HeaderProtectionKey"] == "hPk="
        assert proto["ContentPaddingAddition"] == "24"
        assert proto["CPS"] == "signature"

    def test_version_31_when_trailers_present(self):
        proto = self._proto_section(AWG_V3_CONF + "RandomTrailers = 4\n")["awg3"]
        assert proto["protocol_version"] == "3.1"
        assert proto["RandomTrailers"] == "4"

    def test_no_hint_keeps_legacy_container_name(self):
        # Existing awg/awg2 QR codes must not change: without a hint the
        # container stays amnezia-awg even for 3.x configs (the client
        # determines the version from the config keys, not the name).
        for hint in (None, "awg", "awg2"):
            result = build_amnezia_config(AWG_V3_CONF, hint)
            entry = result["containers"][0]
            assert entry["container"] == "amnezia-awg"
            assert "awg" in entry
            assert "awg3" not in entry

    def test_multi_instance_hint_suffixes_container(self):
        result = build_amnezia_config(AWG_V3_CONF, "awg3__2")
        assert result["containers"][0]["container"] == "amnezia-awg3-2"

    def test_awg3_roundtrip_qr(self):
        qr = generate_qr_content(AWG_V3_CONF, "awg3")
        padded = qr + "=" * (-len(qr) % 4)
        raw = base64.urlsafe_b64decode(padded)
        version = struct.unpack(">I", raw[:4])[0]
        assert version == 0x07C00100
        js = json.loads(zlib.decompress(raw[12:]))
        assert js["defaultContainer"] == "amnezia-awg3"
        proto = js["containers"][0]["awg3"]
        assert proto["protocol_version"] == "3.x"
        last_config = json.loads(proto["last_config"])
        assert last_config["config"].startswith("[Interface]")
