# Deployment & Security Hardening

> Связан: [INDEX](INDEX.md) | [03-infra](03-infrastructure.md)

> Статус: Dockerfile и production compose пока унаследованы от форка и не входят в текущий dev workflow. Проект запускается локально через `venv/bin/uvicorn app:app --reload --port 5000`; `docker-compose.dev.yml` используется только для базы данных. Этот документ — задел для будущего deployment work, а не инструкция для текущей разработки.

## Docker Compose (production)

**Image:** `prvtpro/amnezia-panel:latest`

### Security hardening (docker-compose.yml)

| Параметр | Значение | Назначение |
|---|---|---|
| `read_only: true` | rootfs | Immutable container |
| `tmpfs: /tmp` | tmp | Writable temp dir |
| `cap_drop: [ALL]` | capabilities | Minimum privileges |
| `security_opt: [no-new-privileges]` | — | Prevent privilege escalation |
| `user: panel` (UID 1000) | non-root | `/sbin/nologin` shell |
| `healthcheck` | TCP connect :5000 | Python inline check |

### Entrypoint (docker-entrypoint.sh)

1. Если `AWP_DEV=1` → пропуск проверок
2. Проверка `SECRET_KEY` → exit 1 если пуст
3. Проверка `MASTER_KEY` → exit 1 если пуст
4. `exec "$@"` → CMD

### Генерация секретов (перед деплоем)

```bash
# SECRET_KEY (минимум 32 символа)
openssl rand -base64 48

# MASTER_KEY (валидный Fernet ключ)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## WARP variant

**Dockerfile.warp** — `python:3.14-slim-bookworm` (не 3.12!)

`docker-entrypoint-warp.sh`:
1. Проверка `warp-svc` binary
2. Запуск `warp-svc` в background → `/tmp/warp-svc.log`
3. Wait до 10с для `warp-cli status`
4. `exec "$@"`

WARP Docker Compose:
- `sysctls: net.ipv6.conf.all.disable_ipv6=0` — IPv6 required for WARP
- Дополнительные capabilities для WARP daemon

## SSL mode

`_startup()` → читает SSL settings из БД. Если `ssl.enabled` + inline cert/key:
- Записывает PEM в `ssl_temp/` директорию
- Передаёт в `uvicorn.run(ssl_certfile=..., ssl_keyfile=...)`

## Default admin

`UserRepository.ensure_default_admin()` (`repositories.py`):
- Создаёт `admin` / `admin` **только** если таблица users пуста
- Role: `admin`, enabled: `True`

## Plaintext secret detection (audit)

Секреты серверов хранятся в БД (колонки `Server.password`, `Server.private_key`), шифруются Fernet через `managers/secrets.py`. Признак незашифрованного значения — отсутствие префикса `v1:`. Проверка на уровне приложения: `encrypt_secret()` идемпотентен и возвращает `v1:<token>`.

## Protocol Extension — контракт (не пошаговая инструкция)

Новый VPN-протокол подключается через duck-typed интерфейс (без базового класса). Точки интеграции:

- **Менеджер** в `managers/<name>_manager.py` — реализует требуемые call sites methods: `check_*`, `install_protocol`, `remove_container`, client/config/status methods. Единого формального interface нет; нужно задать сигнатуры и добавить contract tests. `AWGManager` полезен как multi-instance reference, но не как универсальный шаблон сигнатур.
- **Реестр** в `app.py`: `BASE_PROTOCOLS`, при multi-instance — `MULTI_INSTANCE_PROTOCOLS`, при клиентском трафике — `CLIENT_TRAFFIC_BASES`, ветка в `get_protocol_manager()`.
- **Имена/названия**: `protocol_container_name()` и `protocol_display_name()`.
- **Конфиг** в `amnezia_config.py` — если протокол отдаёт конфиг Amnezia-клиенту.
- **Трафик**: если протокол учитывает трафик, добавить его в `CLIENT_TRAFFIC_BASES` (см. [02-core](02-core.md)) и убедиться, что менеджер реализует `get_traffic_snapshot`/`get_clients`.
- **Telegram**: callback-обработчики в `telegram_bot.py`.
- **Тесты**: моки SSH в `tests/managers/`, без реальных сокетов.

### Реестр протоколов (после порта upstream)

| Base | Display | Container | Multi-instance | Клиентский трафик |
|---|---|---|---|---|
| `awg` | AmneziaWG | `amnezia-awg[-N]` | ✅ | ✅ |
| `awg2` | AmneziaWG 2.0 | `amnezia-awg2[-N]` | ✅ | ✅ |
| `awg3` | AmneziaWG 3.1 | `amnezia-awg3[-N]` | ✅ | ✅ |
| `awg_legacy` | AmneziaWG Legacy | `amnezia-awg-legacy[-N]` | ✅ | ✅ |
| `wireguard` | WireGuard | `amnezia-wireguard` | ❌ | ✅ |
| `xray` | Xray | `amnezia-xray[-N]` | ✅ | ✅ |
| `telemt` | Telemt | `telemt[-N]` | ✅ | ✅ |
| `socks5` | SOCKS5 | `amnezia-socks5proxy[-N]` | ✅ | ❌ |
| `dns` | AmneziaDNS | `amnezia-dns` | ❌ | ❌ |
| `adguard` | AdGuard Home | `amnezia-adguard` | ❌ | ❌ |
| `nginx` | NGINX | `amnezia-nginx` | ❌ | ❌ |

Все AWG-базы (`awg`/`awg2`/`awg3`) обслуживает один `AWGManager`, версия выбирается по base из protocol key. Install-эндпоинт (`routers/protocols.py`) для AWG-баз принимает `mtu`/`dns`/`dns6`/`special_junk` и выполняет **used-port check**: явный `port`, занятый другим протоколом сервера, → 400 `Port N is already used by <proto>`.

## Отклонения от upstream-порта (зафиксированные решения)

1. **`warp_service.py` + `managers/split_tunnel.py` портированы** — незадекларированные зависимости `awg_manager` из upstream. Оба stdlib-only, покрыты unit-тестами без SSH/Docker.
2. **Маппинг контейнера по `protocol_hint` отменён.** Исходный план предполагал, что `awg3`-hint в `amnezia_config` переименует контейнер в `amnezia-awg3`; реальный клиент Amnezia не знает ключ `awg3` и показывает «Not Installed». Upstream держит `amnezia-awg` для всех версий AWG (см. [06-vpn-formats](06-vpn-formats.md)).
3. **`xray_manager` портирован полностью (WP3.5)** — `XRAY_CORE_VERSION=v26.10.31`, сборка образа через `_dockerfile_content()`, `rename_client`, `public_port` в конфигах, compat `docker_container_state`; adu-payload с `port`/`listen` и `"Added 0 user"` как ошибка сохранены.

## Smoke Test Checklist (кратко)

| # | Проверка | Команда |
|---|---|---|
| 1 | Контейнер запущен и healthy | `docker ps` |
| 2 | Логин работает | POST `/api/auth/login` |
| 3 | Сервер добавляется | POST `/api/servers/add` |
| 4 | Протокол устанавливается | POST `/api/servers/{id}/install` |
| 5 | Подключение создаётся | POST `/api/servers/{id}/connections/add` |
| 6 | Share-ссылка работает | GET `/share/{token}` |
| 7 | Нет plaintext secrets | Проверить `v1:` префикс в БД |
| 8 | Сессия переживает рестарт | Перезапуск контейнера |
| 9 | Rate limiting работает | 7-й login → 429 |
| 10 | CSRF блокирует без токена | POST без `X-CSRF-Token` → 403 |
| 11 | Backup создаётся | Проверка `backups/` директории |
