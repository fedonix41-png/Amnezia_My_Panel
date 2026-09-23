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
- **Реестр** в `app.py`: `BASE_PROTOCOLS`, при multi-instance — `MULTI_INSTANCE_PROTOCOLS`, ветка в `get_protocol_manager()`.
- **Имена/названия**: `protocol_container_name()` и `protocol_display_name()`.
- **Конфиг** в `amnezia_config.py` — если протокол отдаёт конфиг Amnezia-клиенту.
- **Трафик**: если протокол учитывает трафик, добавить его в список `_scrape_server_traffic()` (см. [03-infra](03-infrastructure.md)).
- **Telegram**: callback-обработчики в `telegram_bot.py`.
- **Тесты**: моки SSH в `tests/managers/`, без реальных сокетов.

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
