"""Build AmneziaVPN native JSON configs from raw client config text.

The AmneziaVPN mobile/desktop client only recognises QR codes and ``vpn://``
links that carry its native JSON format (plain, qCompress-compressed, or
base64-encoded JSON).  Raw WireGuard/AWG ``.conf`` text or ``vless://``
URLs are silently ignored by the QR scanner (``extractConfigFromQr``).

This module bridges that gap: it parses our raw configs, builds the native
JSON structure (``containers`` / ``last_config``), and provides the two
encoders (``generate_qr_content``, ``generate_vpn_link``) that the rest of
the codebase needs.

Pure functions only — no SSH, no I/O, no side effects.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import zlib
from urllib.parse import urlparse, parse_qs, unquote

_WG_DEFAULT_PORT = 55424
_DEFAULT_DNS1 = "1.1.1.1"
_DEFAULT_DNS2 = "1.0.0.1"
_DEFAULT_MTU = "1280"
_DEFAULT_KEEPALIVE = "25"
_DESCRIPTION = "Amnezia Web Panel"

_AWG_REQUIRED_JUNK = {"Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"}
_AWG_I_FIELDS = ("I1", "I2", "I3", "I4", "I5")

_AWG_JUNK_FIELDS = (
    "Jc", "Jmin", "Jmax",
    "S1", "S2", "S3", "S4",
    "H1", "H2", "H3", "H4",
    "I1", "I2", "I3", "I4", "I5",
    "CPS",
)


def qt_compress(data: bytes, level: int = 8) -> bytes:
    """Replicate Qt's ``qCompress``: 4-byte big-endian uncompressed length
    prefix followed by the raw zlib stream.

    The AmneziaVPN client calls ``qUncompress`` which reads the 4-byte
    length, allocates a buffer, then inflates the zlib data.
    """
    return struct.pack(">I", len(data)) + zlib.compress(data, level)


def base64url_encode(data: bytes) -> str:
    """base64url without trailing ``=`` (matches Qt's ``Base64UrlEncoding |
    OmitTrailingEquals``)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _parse_wg_config(config_text: str) -> dict[str, str] | None:
    """Parse a WireGuard/AWG ``.conf`` into a flat ``Key -> Value`` dict.

    Mirrors ``importController.cpp::extractWireGuardConfig``: split on lines,
    skip section headers (``[Interface]`` / ``[Peer]``), parse ``Key = Value``.
    Returns ``None`` when a required field (``PrivateKey``, ``Address``,
    ``PublicKey``) is missing, the AmneziaVPN client rejects such configs.
    """
    result: dict[str, str] = {}
    for line in config_text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            result[key] = value

    required = {"PrivateKey", "Address", "PublicKey"}
    if not required.issubset(result):
        return None
    return result


def _parse_vless_url(url: str) -> dict[str, str] | None:
    """Parse a ``vless://`` URL into its component fields."""
    if not url.startswith("vless://"):
        return None
    parsed = urlparse(url)
    uuid = unquote(parsed.username or "")
    host = parsed.hostname or ""
    port = str(parsed.port) if parsed.port else ""
    if not uuid or not host:
        return None

    qs = parse_qs(parsed.query)

    def _first(key: str) -> str:
        vals = qs.get(key, [])
        return unquote(vals[0]) if vals else ""

    return {
        "uuid": uuid,
        "host": host,
        "port": port,
        "pbk": _first("pbk"),
        "sni": _first("sni"),
        "sid": _first("sid"),
        "flow": _first("flow") or "xtls-rprx-vision",
        "fp": _first("fp") or "chrome",
        "name": unquote(parsed.fragment) if parsed.fragment else "",
    }


def _split_endpoint(endpoint: str) -> tuple[str, int]:
    """Split ``host:port`` on the last colon. Default port 55424."""
    if ":" in endpoint:
        host, _, port_str = endpoint.rpartition(":")
        try:
            return host, int(port_str)
        except ValueError:
            return endpoint, _WG_DEFAULT_PORT
    return endpoint, _WG_DEFAULT_PORT


def _extract_dns(config_map: dict[str, str], raw_config: str) -> tuple[str, str]:
    """Extract two DNS servers from the config. Falls back to 1.1.1.1 / 1.0.0.1."""
    dns_line = config_map.get("DNS", "")
    if dns_line:
        parts = [d.strip() for d in dns_line.split(",")]
        dns1 = parts[0] if parts and parts[0] else _DEFAULT_DNS1
        dns2 = parts[1] if len(parts) > 1 and parts[1] else _DEFAULT_DNS2
        return dns1, dns2
    match = re.search(
        r"DNS\s*=\s*(\d+\.\d+\.\d+\.\d+).*(\d+\.\d+\.\d+\.\d+)", raw_config
    )
    if match:
        return match.group(1), match.group(2)
    return _DEFAULT_DNS1, _DEFAULT_DNS2


def _detect_awg_version(config_map: dict[str, str]) -> str | None:
    """Determine ``protocol_version`` for AWG configs.

    * S3 **and** S4 present     -> ``"2"``
    * I1-I5 without S3/S4       -> ``"1.5"``
    * otherwise                 -> ``None``
    """
    if "S3" in config_map and "S4" in config_map:
        return "2"
    if any(f in config_map for f in _AWG_I_FIELDS):
        return "1.5"
    return None


def _build_wg_json(
    config_map: dict[str, str],
    raw_config: str,
) -> dict | None:
    """Build the Amnezia native JSON for a WireGuard or AWG config.

    Structure matches the working PHP QrUtil::buildOldEnvelopeFromConf():
    - AWG obfuscation params (H1-H4, Jc, Jmin, Jmax, S1, S2) are placed
      BOTH at the top level of the protocol section AND inside last_config.
    - last_config always includes clientId / client_pub_key (empty string
      when not derivable).
    - isThirdPartyConfig is NOT set -- the PHP reference project omits it.
    - last_config is pretty-printed JSON (indent=4) matching PHP behaviour.
    """
    is_awg = _AWG_REQUIRED_JUNK.issubset(config_map)
    proto_key = "awg" if is_awg else "wireguard"
    container = f"amnezia-{proto_key}"

    host_name, port = _split_endpoint(config_map.get("Endpoint", ""))

    allowed_ips_raw = config_map.get("AllowedIPs", "0.0.0.0/0, ::/0")
    allowed_ips = [ip.strip() for ip in allowed_ips_raw.split(",") if ip.strip()]

    # All AWG junk fields -- always emitted (empty string when absent), like PHP
    _LC_AWG = ("H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5",
               "Jc", "Jmax", "Jmin", "S1", "S2", "S3", "S4")

    last_config: dict = {}
    for f in _LC_AWG:
        last_config[f] = config_map.get(f, "")

    last_config.update({
        "allowed_ips": allowed_ips,
        "clientId": "",
        "client_ip": config_map.get("Address", ""),
        "client_priv_key": config_map.get("PrivateKey", ""),
        "client_pub_key": "",
        "config": raw_config.strip(),
        "hostName": host_name,
        "mtu": config_map.get("MTU", _DEFAULT_MTU),
        "persistent_keep_alive": config_map.get("PersistentKeepalive", _DEFAULT_KEEPALIVE),
        "port": port,
        "psk_key": config_map.get("PresharedKey", ""),
        "server_pub_key": config_map.get("PublicKey", ""),
    })

    # PHP puts these AWG params at the TOP level of the proto section too
    proto_section: dict = {
        "H1": config_map.get("H1", ""),
        "H2": config_map.get("H2", ""),
        "H3": config_map.get("H3", ""),
        "H4": config_map.get("H4", ""),
        "Jc": config_map.get("Jc", ""),
        "Jmax": config_map.get("Jmax", ""),
        "Jmin": config_map.get("Jmin", ""),
        "S1": config_map.get("S1", ""),
        "S2": config_map.get("S2", ""),
        "last_config": json.dumps(last_config, ensure_ascii=False,
                                  indent=4, separators=(",", ": ")),
        "port": str(port),
        "transport_proto": "udp",
    }

    protocol_version = _detect_awg_version(config_map) if is_awg else None
    if protocol_version is not None:
        proto_section["protocol_version"] = protocol_version
        for f in ("I1", "I2", "I3", "I4", "I5", "S3", "S4"):
            proto_section[f] = config_map.get(f, "")

    dns1, dns2 = _extract_dns(config_map, raw_config)

    # PHP: protocol key comes BEFORE 'container' in the dict
    container_entry: dict = {proto_key: proto_section, "container": container}

    return {
        "containers": [container_entry],
        "defaultContainer": container,
        "description": _DESCRIPTION,
        "dns1": dns1,
        "dns2": dns2,
        "hostName": host_name,
    }


def _build_xray_json(parsed: dict[str, str]) -> dict:
    """Build the Amnezia native JSON for an Xray VLESS-Reality config."""
    port = int(parsed["port"]) if parsed.get("port") else 443

    client_json = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "port": "socks",
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"udp": True},
            "tag": "socks-in",
        }],
        "outbounds": [{
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": parsed["host"],
                    "port": port,
                    "users": [{
                        "encryption": "none",
                        "flow": parsed.get("flow") or "xtls-rprx-vision",
                        "id": parsed["uuid"],
                    }],
                }],
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "fingerprint": parsed.get("fp") or "chrome",
                    "publicKey": parsed.get("pbk", ""),
                    "serverName": parsed.get("sni", ""),
                    "shortId": parsed.get("sid", ""),
                    "spiderX": "/",
                },
            },
            "tag": "vless-out",
        }],
    }

    return {
        "containers": [{
            "xray": {
                "last_config": json.dumps(client_json, ensure_ascii=False,
                                          indent=4, separators=(",", ": ")),
                "port": str(port),
                "transport_proto": "tcp",
            },
            "container": "amnezia-xray",
        }],
        "defaultContainer": "amnezia-xray",
        "description": _DESCRIPTION,
        "dns1": _DEFAULT_DNS1,
        "dns2": _DEFAULT_DNS2,
        "hostName": parsed["host"],
    }


def build_amnezia_config(
    config_text: str, protocol_hint: str | None = None
) -> dict | None:
    """Build AmneziaVPN native JSON from raw config text.

    Auto-detects format: WireGuard/AWG (.conf) or Xray (vless://).
    Returns ``None`` for unrecognized formats (e.g. TeleMT).
    """
    if not config_text:
        return None
    text = config_text.strip()

    if text.startswith("vless://"):
        parsed = _parse_vless_url(text)
        if not parsed:
            return None
        return _build_xray_json(parsed)

    if "[Interface]" in text:
        config_map = _parse_wg_config(text)
        if not config_map:
            return None
        return _build_wg_json(config_map, text)

    return None


def generate_qr_content(
    config_text: str, protocol_hint: str | None = None
) -> str:
    """QR code content for Amnezia client scanner.
    
    The Amnezia scanner expects Qt QDataStream serialization:
    Version magic (0x07C00200) + QByteArray(qCompress(JSON)).
    It does not expect the ``vpn://`` prefix.
    """
    config = build_amnezia_config(config_text, protocol_hint)
    if not config:
        return ""
    
    raw = json.dumps(config, ensure_ascii=False, indent=4, separators=(",", ": ")).encode("utf-8")
    compressed = qt_compress(raw)
    
    version = 0x07C00200
    length = len(compressed)
    # Pack version (uint32) and QByteArray length (uint32)
    envelope = struct.pack(">II", version, length) + compressed
    return base64url_encode(envelope)


def generate_vpn_link(
    config_text: str, protocol_hint: str | None = None
) -> str:
    """``vpn://`` text link.
    
    Used when clicking a shareable link. The app parses it differently
    than the QR scanner and expects ``vpn://`` + base64url(qCompress(JSON)).
    """
    config = build_amnezia_config(config_text, protocol_hint)
    if not config:
        return ""
    
    raw = json.dumps(config, ensure_ascii=False, indent=4, separators=(",", ": ")).encode("utf-8")
    encoded = base64url_encode(qt_compress(raw))
    return f"vpn://{encoded}"
