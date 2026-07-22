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
        # Base64url of 0x07C00200 starts with B8ACAA
        assert qr.startswith("B8ACAA")

    def test_returns_base64url_compressed_json(self):
        qr = generate_qr_content(AWG_V2_CONF, "awg")
        padded = qr + "=" * (-len(qr) % 4)
        raw = base64.urlsafe_b64decode(padded)
        import struct, zlib
        version, length = struct.unpack(">II", raw[:8])
        assert version == 0x07C00200
        assert length == len(raw) - 8
        uncompressed_len = struct.unpack(">I", raw[8:12])[0]
        data = zlib.decompress(raw[12:])
        assert len(data) == uncompressed_len
        js = json.loads(data)
        assert "containers" in js

    def test_xray_qr_decodable(self):
        qr = generate_qr_content(VLESS_URL, "xray")
        padded = qr + "=" * (-len(qr) % 4)
        raw = base64.urlsafe_b64decode(padded)
        import struct, zlib
        data = zlib.decompress(raw[12:])
        js = json.loads(data)
        assert js["containers"][0]["container"] == "amnezia-xray"


class TestGenerateVpnLink:
    def test_returns_empty_string_for_invalid(self):
        assert generate_vpn_link("invalid", "telemt") == ""

    def test_different_from_qr_content(self):
        """QR content and vpn:// link are different."""
        qr = generate_qr_content(AWG_V2_CONF, "awg")
        link = generate_vpn_link(AWG_V2_CONF, "awg")
        assert link != qr
        assert link.startswith("vpn://")

class TestIntegration:
    def test_awg_qr_decodable_by_amnezia_client_path(self):
        qr = generate_qr_content(AWG_V2_CONF, "awg")
        padded = qr + "=" * (-len(qr) % 4)
        raw = base64.urlsafe_b64decode(padded)
        import struct, zlib
        # client reads quint32 version
        v = struct.unpack(">I", raw[:4])[0]
        assert v == 0x07C00200
        # next is QByteArray, which starts with quint32 length
        length = struct.unpack(">I", raw[4:8])[0]
        assert length == len(raw) - 8
        
        # then qUncompress it
        uncompressed_len = struct.unpack(">I", raw[8:12])[0]
        data = zlib.decompress(raw[12:])
        js = json.loads(data)
        assert js["defaultContainer"] == "amnezia-awg"

    def test_wireguard_qr_decodable(self):
        qr = generate_qr_content(WG_PLAIN_CONF, "wireguard")
        padded = qr + "=" * (-len(qr) % 4)
        raw = base64.urlsafe_b64decode(padded)
        import struct, zlib
        data = zlib.decompress(raw[12:])
        js = json.loads(data)
        assert js["defaultContainer"] == "amnezia-wireguard"
