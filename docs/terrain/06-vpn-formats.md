# VPN Config Formats (amnezia_config.py, 395 строк)

> Связан: [INDEX](INDEX.md) | [02-core](02-core.md) | [03-infra](03-infrastructure.md)

**Модуль `managers/amnezia_config.py`** — чистые функции, без I/O. Мост между raw WireGuard/Xray конфигами и Amnezia native JSON.

⚠️ **Amnezia-клиенты распознают ТОЛЬКО native JSON** — raw `.conf` и `vless://` молча игнорируются QR-сканером.

## Точки входа

| Функция | Назначение |
|---|---|
| `build_amnezia_config(text, hint)` | Автодетект формата → native JSON |
| `generate_vpn_link(text, hint)` | `vpn://` URI для шеринга |
| `generate_qr_content(text, hint)` | Base64url для QR-кода |

## Автодетект

- Начинается с `vless://` → VLESS-Reality парсинг (`_parse_vless_url`)
- Содержит `[Interface]` → WireGuard/AWG парсинг (`_parse_wg_config`)
- Иначе → `None`

## AWG version detection

`_detect_awg_version()`:

| Условие | Версия |
|---|---|
| S3 + S4 present | `"2"` (AWG 2.0) |
| I1-I5 present, S3/S4 absent | `"1.5"` |
| Иначе | `None` (legacy WireGuard) |

### AWG 2.0 obfuscation params

| Параметр | Описание |
|---|---|
| Jc, Jmin, Jmax | Junk packet counters |
| S1, S2 | Special packet sizes |
| S3, S4 | **AWG 2.0 only** — additional sizes |
| H1, H2, H3, H4 | Header init values. В 2.0 — **non-overlapping ranges** |
| I1-I5, CPS | **AWG 1.5** — init packet chain + CPS |

`generate_awg_params(use_ranges=True)` в `managers/awg_manager.py` — для AWG 2.0 генерирует H как непересекающиеся диапазоны.

## vpn:// link format

```
vpn:// + base64url( uint32(uncompressed_len) + zlib(json) )
```

- 4-byte big-endian длина оригинала + zlib-сжатый JSON
- Base64url без trailing `=`

## QR code format

Reverse-engineered под Amnezia C++ `extractConfigFromQr` decoder:

```
16-byte header:
  uint32 version = 0x07C00100
  uint32 compressed_len + 4  (Qt QByteArray convention)
  uint32 raw_json_len
  uint32 reserved (0)
+ zlib(compressed JSON)
→ base64url encode
```

`qt_compress()` реплицирует Qt `qCompress`: 4-byte big-endian length + zlib stream.

## Native JSON structure (упрощённо)

```json
{
  "protocol": "awg",
  "dns1": "1.1.1.1", "dns2": "1.0.0.1",
  "hostName": "<server_ip>",
  "port": 55424,
  "clientId": "<uuid>",
  "clientPrivateKey": "...",
  "serverPublicKey": "...",
  "pskKey": "...",
  "configData": {
    "Jc": 3, "Jmin": 50, "Jmax": 1000,
    "S1": 17, "S2": 83, "S3": 44, "S4": 61,
    "H1": "...", "H2": "...", "H3": "...", "H4": "..."
  },
  "last_config": "<raw .conf text>"
}
```

## Где используется

- `generate_vpn_link()` из `app.py` — вызывается при add/edit connection
- `generate_qr_content()` из `app.py` — для QR-кода в UI и Telegram
- `telegram_bot.py` — config delivery: AWG/WG → `vpn://`, Xray/Telemt → raw link
- `routers/connections.py` — возвращает `vpn_link` и `qr_content` в API response

## Что НЕ покрывается

- Telemt (MTProto) — `build_amnezia_config` возвращает `None` для tg:// ссылок
- SOCKS5 — нет native JSON формата, отдаётся как `socks5://user:pass@host:port`
- DNS/AdGuard/Nginx — не VPN-протоколы
