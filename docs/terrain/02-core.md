# Ядро (Domain/Core)

> Связан: [INDEX](INDEX.md) | [01-entry](01-entry-points.md) | [05-gotchas](05-gotchas.md)

## Реестр протоколов

```python
BASE_PROTOCOLS = ['awg', 'awg2', 'awg3', 'awg_legacy', 'xray', 'telemt', 'dns',
                  'wireguard', 'socks5', 'adguard', 'nginx']
MULTI_INSTANCE_PROTOCOLS = {'awg','awg2','awg3','awg_legacy','xray','telemt','socks5'}
CLIENT_TRAFFIC_BASES = ('awg', 'awg2', 'awg3', 'awg_legacy', 'xray', 'telemt', 'wireguard')
```

Multi-instance: ключи `awg`, `awg__2`, `awg__3` (база + `__` + номер).

| Утилита | Расположение | Назначение |
|---|---|---|
| `protocol_base(p)` | `app.py` | `awg__2` → `awg` |
| `protocol_instance(p)` | `app.py` | `awg__2` → `2` (default 1) |
| `protocol_key(base, inst)` | `app.py` | `awg` + `2` → `awg__2` |
| `next_protocol_key(protos, base)` | `app.py` | Следующий свободный instance |
| `protocol_display_name(p)` | `app.py` | `awg` → `AmneziaWG`, `awg3` → `AmneziaWG 3.1` |
| `protocol_container_name(p)` | `app.py` | `awg__2` → `amnezia-awg-2`, `awg3` → `amnezia-awg3` |
| `is_valid_protocol(p)` | `app.py` | Проверка по `BASE_PROTOCOLS` |

`CLIENT_TRAFFIC_BASES` — единственный список протоколов, чьи менеджеры отдают per-peer счётчики (`get_clients`/`get_traffic_snapshot`). Фоновый traffic sync (`_scrape_server_traffic`) фильтрует по нему и пропускает service-протоколы (dns/socks5/adguard/nginx), которые иначе молча упали бы на fallback к `AWGManager`.

## Фабрика менеджеров

`get_protocol_manager(ssh, protocol)` в `app.py`. **Все импорты deferred** (внутри функции).

| base_protocol | Класс | Модуль |
|---|---|---|
| `awg`, `awg2`, `awg3`, `awg_legacy` | `AWGManager` | `managers.awg_manager` |
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
- Остальные: передаёт `protocol` первым аргументом, но это корректно только для methods с соответствующей сигнатурой
- `getattr(manager, method)` — динамический dispatch

`_manager_call` не является полной адаптер-абстракцией. `install_protocol` и `remove_container` в `routers/protocols.py` вызываются по явным веткам. Xray хранит protocol key в manager instance, но его client methods имеют смешанные сигнатуры. Перед рефакторингом проверяй method definition и все call sites.

## Контракты менеджеров (порт upstream v1.8.10)

### client methods с `public_port`

`AWGManager` и `WireGuardManager` принимают опциональный `public_port` — порт, который клиенты набирают, когда он отличается от instance-порта (port sharing / NAT):

- `AWGManager.add_client(protocol_type, client_name, server_host, port, public_port=None, ...)`
- `AWGManager.get_client_config(protocol_type, client_id, server_host, port, public_port=None, ...)`
- `WireGuardManager.add_client(client_name, server_host, public_port=None)` (instance-порт менеджер знает сам)
- `WireGuardManager.get_client_config(client_id, server_host, port=None, public_port=None)`

`Endpoint = {server_host}:{public_port or port}`. `TelemtManager.get_client_config` также принимает опциональный `public_port` (fallback-ссылки набирают его вместо instance-порта); остальные менеджеры `public_port` не принимают.

### TelemtManager (порт upstream v1.8.10)

- Fallback-ссылки собирает `_build_client_link(secret, host, port, config_text)`: при `tls_emulation` в конфиге секрет получает FakeTLS-паддинг `ee<secret><hex(tls_domain)>`; без домена — `RuntimeError`. API-ссылки telemt отдаёт как есть.
- `_api_request` строит curl через `shlex.quote` каждого аргумента с `--` перед URL и `--globoff`.
- `rename_client(protocol_type, client_id, new_name)` — имя = identity: переписывает все `username = ...` строки в `access.*` секциях (сохраняя `#`-маркер отключённых), пересоздаёт runtime-пользователя через API (DELETE + POST).
- `_patch_public_links` / `set_public_endpoint` — управление `public_host`/`public_port` в `[general.links]` без переустановки.
- `check_protocol_installed`/`check_container_running` поддерживают опциональный `ssh.docker_container_state` (тот же compat-паттерн, что в AWG/WG/Xray/SOCKS5/AdGuard/Nginx).

### Поля `get_server_status` (AWG/WG)

Помимо legacy-полей (`container_exists`, `container_running`, `port`, `awg_params`, `clients_count`) статус AWG/WG содержит:

| Поле | Тип | Назначение |
|---|---|---|
| `protocol_version` | str/нет | Версия AWG по ключам конфига: `'3.1'` (RandomTrailers/DisableCookies), `'3.x'` (HeaderProtectionKey), `'2.0'` (base awg2), иначе отсутствует (awg = 1.5) |
| `header_protection` | bool | Наличие `HeaderProtectionKey` в конфиге |
| `external_count` | int | Пиры из `awg0.conf`, отсутствующие в clientsTable (нативные клиенты Amnezia); учитываются в `clients_count` |

Версию определяют ТОЛЬКО ключи конфига — контейнерное имя/номер сборки недостоверны (официальный клиент ставит AWG 3.x в `amnezia-awg2`).

### Helper-модули (портированы с upstream)

| Модуль | Назначение |
|---|---|
| `managers/clients_table.py` | Атомарная запись clientsTable: приватный staging в /tmp, `docker cp` + rename внутри контейнера, бэкап предыдущей версии `clientsTable.panel-backup`; вызывающий код держит RMW-lock |
| `managers/clients_table_reader.py` | Strict read-only чтение clientsTable |
| `managers/traffic_counters.py` | `read_transfer_snapshot(ssh, container, binary, interface)` — маркер-фреймированный `show transfer` (`COUNTERS_V1`), точные int-счётчики без cache fallback; см. [03-infra](03-infrastructure.md) |
| `managers/awg_plugin_updater.py` | Обновление компонентов AWG (kernel module через DKMS, userspace-контейнер): preflight, бэкап, атомарная замена, rollback; stdlib-only, лог шагов для роутера |
| `managers/backup_manager.py` | Протокольные бэкапы: create/list на сервере + restore/upload/delete с локальной валидацией архива (`inspect_archive` режет tar-slip/absolute/symlink-escape до любого remote-extract); вызывающий код подключает SSH |
| `services/server_stats.py` | Сбор host-статистики (uptime/load/memory/disk/network) для эндпоинта stats сервера; вызывается из `routers/servers.py` |
| `warp_service.py` | Чистая логика Cloudflare WARP (state store, MTU budget, quota); SSH-оркестрация в `app.py` |
| `managers/split_tunnel.py` | Split-tunnel AllowedIPs: список сетей, которые обходят VPN, вшивается в каждый клиентский конфиг |

`warp_service.py` и `split_tunnel.py` — незадекларированные зависимости `awg_manager` из upstream, портированы послойно в этапе 2 (оба stdlib-only, тестируются без SSH/Docker).

## Криптография

| Функция | Расположение | Алгоритм |
|---|---|---|
| `hash_password()` | `app.py` | PBKDF2-HMAC-SHA256, 600k итераций, random salt, формат `iterations$salt$hash` (3 части) |
| `verify_password()` | `app.py` | Разбирает текущий 3-part и legacy 2-part форматы |
| `encrypt_secret()` | `managers/secrets.py` | Fernet, формат `v1:<token>`, идемпотентен |
| `decrypt_secret()` | `managers/secrets.py` | Decrypt `v1:` prefix, plaintext passthrough |
| `_hash_api_token()` | `app.py` | SHA-256 для API-токенов |
| `_generate_api_token()` | `app.py` | `awp_` + 32-byte URL-safe |

⚠️ **Дублирование:** `app.py` и `database/repositories.py` имеют отдельные `hash_password`. Сейчас оба используют 600k итераций и формат `iterations$salt$hash`; при изменении параметров их нужно менять согласованно.

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
