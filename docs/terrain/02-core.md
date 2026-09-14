# Ядро (Domain/Core)

> Связан: [INDEX](INDEX.md) | [01-entry](01-entry-points.md) | [05-gotchas](05-gotchas.md)

## Реестр протоколов

```python
BASE_PROTOCOLS = ['awg', 'awg2', 'awg_legacy', 'xray', 'telemt', 'dns',
                  'wireguard', 'socks5', 'adguard', 'nginx']
MULTI_INSTANCE_PROTOCOLS = {'awg','awg2','awg_legacy','xray','telemt','socks5'}
```

Multi-instance: ключи `awg`, `awg__2`, `awg__3` (база + `__` + номер).

| Утилита | Расположение | Назначение |
|---|---|---|
| `protocol_base(p)` | `app.py` | `awg__2` → `awg` |
| `protocol_instance(p)` | `app.py` | `awg__2` → `2` (default 1) |
| `protocol_key(base, inst)` | `app.py` | `awg` + `2` → `awg__2` |
| `next_protocol_key(protos, base)` | `app.py` | Следующий свободный instance |
| `protocol_display_name(p)` | `app.py` | `awg` → `AmneziaWG` |
| `protocol_container_name(p)` | `app.py` | `awg__2` → `amnezia-awg-2` |
| `is_valid_protocol(p)` | `app.py` | Проверка по `BASE_PROTOCOLS` |

## Фабрика менеджеров

`get_protocol_manager(ssh, protocol)` в `app.py`. **Все импорты deferred** (внутри функции).

| base_protocol | Класс | Модуль |
|---|---|---|
| `awg`, `awg2`, `awg_legacy` | `AWGManager` | `managers.awg_manager` |
| `xray` | `XrayManager` | `managers.xray_manager` |
| `telemt` | `TelemtManager` | `managers.telemt_manager` |
| `wireguard` | `WireGuardManager` | `managers.wireguard_manager` |
| `socks5` | `Socks5Manager` | `managers.socks5_manager` |
| `adguard` | `AdguardManager` | `managers.adguard_manager` |
| `nginx` | `NginxManager` | `managers.nginx_manager` |
| `dns` | `DNSManager` | `managers.dns_manager` |

## Диспетчеризация вызовов

`_manager_call(manager, method, protocol, *args, **kwargs)` в `app.py`:
- **WireGuardManager**: пропускает `protocol` аргумент (историческое исключение)
- Остальные: `protocol_type` первым аргументом
- `getattr(manager, method)` — динамический dispatch

## Криптография

| Функция | Расположение | Алгоритм |
|---|---|---|
| `hash_password()` | `app.py` | PBKDF2-HMAC-SHA256, 600k итераций, random salt, формат `iterations$salt$hash` (3 части) |
| `verify_password()` | `app.py` | Разбирает 3-part (`iterations$salt$hash`) и 2-part (`salt$hash`), по умолчанию 100k итераций |
| `encrypt_secret()` | `managers/secrets.py` | Fernet, формат `v1:<token>`, идемпотентен |
| `decrypt_secret()` | `managers/secrets.py` | Decrypt `v1:` prefix, plaintext passthrough |
| `_hash_api_token()` | `app.py` | SHA-256 для API-токенов |
| `_generate_api_token()` | `app.py` | `awp_` + 32-byte URL-safe |

⚠️ **Дублирование:** `database/repositories.py` тоже имеет `hash_password`, но с **100k** итераций и форматом `salt$hash` (2 части). `verify_password()` читает оба — новые пароли всегда через версию `app.py`. См. [05-gotchas](05-gotchas.md).

## Конфиг-генерация VPN

| Функция | Файл | Назначение |
|---|---|---|
| `generate_vpn_link()` | `app.py` → `managers/amnezia_config.py` | `vpn://` URI (base64url zlib JSON) |
| `generate_qr_content()` | `app.py` → `managers/amnezia_config.py` | QR-код (B8AB format, zlib) |
| `build_amnezia_config()` | `managers/amnezia_config.py` | Автодетект WG/AWG/VLESS → native JSON |
| `_detect_awg_version()` | `managers/amnezia_config.py` | S3+S4 → "2", I1-I5 → "1.5" |

`amnezia_config.py` — **чистые функции**, без SSH/IO/сайд-эффектов.

## i18n

| Компонент | Расположение | Описание |
|---|---|---|
| `load_translations()` | `app.py` | Читает `translations/*.json` → `TRANSLATIONS` dict |
| `_t(text_id, lang)` | `app.py` | Перевод по ключу, fallback на ID |
| Языки | — | `en`, `ru`, `fa`, `fr`, `zh` (JSON, flat keys) |

## config.py — ключевые переменные

| Переменная | Env | Default (dev/prod) | Валидация |
|---|---|---|---|
| `AWP_DEV` | `AWP_DEV` | `False` | — |
| `SECRET_KEY` | `SECRET_KEY` | eph. / **обяз.** ≥32 | `sys.exit(1)` в prod |
| `MASTER_KEY` | `MASTER_KEY` | eph. Fernet / **обяз.** | Fernet validate в prod |
| `DATABASE_URL` | `DATABASE_URL` | SQLite / PostgreSQL | — |
| `SESSION_COOKIE_SECURE` | — | `False` / `True` | — |
| `POSTHOG_*` | env | Опционален | Клиент создаётся, только если заданы token и host |
| Rate limits | env | `5/10/30 per min` | — |
| `BACKUP_INTERVAL_HOURS` | env | `6` | — |
