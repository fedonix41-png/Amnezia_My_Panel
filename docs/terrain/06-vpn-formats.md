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

`_detect_awg_version()` (порядок зеркалирует `AWGManager.get_server_status` — версию определяют только ключи конфига):

| Условие | Версия |
|---|---|
| RandomTrailers / DisableCookies с любым непустым значением (вкл. `"off"`) | `"3.1"` |
| HeaderProtectionKey present | `"3.x"` |
| S3 **и** S4 present | `"2"` |
| I1-I5 present, S3/S4 absent | `"1.5"` |
| Иначе | `None` (legacy WireGuard) |

⚠️ **Контейнер в native JSON всегда `amnezia-awg`** (proto key `awg`) для КАЖДОЙ версии AWG — повторение upstream `build_amnezia_config()`. Официальный клиент ставит AWG 3.x в `amnezia-awg2` и определяет версию по ключам конфига, а не по имени контейнера; неизвестный ключ `awg3` в JSON клиент показывает как «Not Installed». `protocol_hint` в сигнатурах `_build_wg_json`/`build_amnezia_config` сохранён только для совместимости вызовов и имя контейнера НЕ меняет.

### AWG 2.0 / 3.x obfuscation params

| Параметр | Описание |
|---|---|
| Jc, Jmin, Jmax | Junk packet counters |
| S1, S2 | Special packet sizes |
| S3, S4 | **AWG 2.0 only** — additional sizes |
| H1, H2, H3, H4 | Header init values. В 2.0 — **non-overlapping ranges** |
| I1-I5, CPS | **AWG 1.5** — init packet chain + CPS |
| HeaderProtectionKey, ContentPaddingAddition, RekeyAfterTime, RekeyTimeout, RejectAfterTime, KeepaliveTimeout, MaxHandshakeAttempts | **AWG 3.x** (`_AWG3_FIELDS`, порядок = `AWG3_PARAM_MAP` в `awg_manager.py`) |
| RandomTrailers, DisableCookies | **AWG 3.1** — маркеры версии (`_AWG3_VERSION_MARKERS`) |

`generate_awg_params(use_ranges=True, awg3=False)` в `managers/awg_manager.py` — для AWG 2.0 генерирует H как непересекающиеся диапазоны; `awg3=True` добавляет HeaderProtectionKey + 3.1-маркеры (по умолчанию `'on'`).

⚠️ **Версия-маркерные ключи эмитятся только при реальном наличии** в конфиге: пустые S3/S4 в proto-секции читаются клиентом как «2.0» и маскируют AWG 1.5; пустые CPS/AWG3-ключи ломают детект 3.x. Отсутствующие ключи → в proto-секцию и `last_config` не попадают вообще.

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
    "H1": "...", "H2": "...", "H3": "...", "H4": "...",
    "protocol_version": "3.1"
  },
  "last_config": "<raw .conf text>"
}
```

`protocol_version` в proto-секцию попадает только для AWG и только когда `_detect_awg_version` вернул не-None.

## Где используется

- `generate_vpn_link()` из `app.py` — вызывается при add/edit connection
- `generate_qr_content()` из `app.py` — для QR-кода в UI и Telegram
- `telegram_bot.py` — config delivery: AWG/WG → `vpn://`, Xray/Telemt → raw link
- `routers/connections.py` — возвращает `vpn_link` и `qr_content` в API response

## Что НЕ покрывается

- Telemt (MTProto) — `build_amnezia_config` возвращает `None` для tg:// ссылок; fallback-ссылки из `TelemtManager._build_client_link` несут FakeTLS-паддинг `ee<secret><hex(tls_domain)>` при `tls_emulation` в конфиге инстанса (см. [02-core §TelemtManager](02-core.md))
- SOCKS5 — нет native JSON формата, отдаётся как `socks5://user:pass@host:port`
- DNS/AdGuard/Nginx — не VPN-протоколы
