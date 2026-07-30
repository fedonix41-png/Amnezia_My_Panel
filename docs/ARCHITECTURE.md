# Amnezia Web Panel — Architecture (SSOT)

> Single Source of Truth для разработчиков и LLM-агентов. Этот документ **загружается по требованию** (Grep/Read), а не автозагружается в каждую сессию — для стартовых правил и индекса см. `AGENTS.md` в корне репозитория.
>
> Все факты проверены по коду; ссылки в формате `file:line`. Если код расходится с этим документом — код прав, обновите документ.

## 1. Технологический стек и запуск

- **Язык**: Python **3.12+** (`Dockerfile:3` — `FROM python:3.12-slim`).
- **Веб-фреймворк**: **FastAPI** (ASGI) поверх **Starlette**.
  - `app.py:29` — `from fastapi import FastAPI, Request, Query, UploadFile, File`
  - `app.py:82` — `app = FastAPI(title="Amnezia Web Panel", openapi_tags=OPENAPI_TAGS, redoc_url=None)`
  - `app.py:92-104` — кастомный `/redoc`, pinned на `redoc@2`, Google Fonts отключены.
- **Сервер приложений**: **uvicorn** (`requirements.txt:36`; `app.py:2250` — `uvicorn.run(...)`).
- **Путь запуска**:
  - `Dockerfile:27-28` — `ENTRYPOINT ["/app/docker-entrypoint.sh"]`, `CMD ["python3", "app.py"]`.
  - `docker-entrypoint.sh:1-24` — валидирует `SECRET_KEY`/`MASTER_KEY`, кроме `AWP_DEV=1`, затем `exec "$@"`.
  - `app.py:2212-2250` — `if __name__ == '__main__':` читает SSL-настройки из `data.json`, вызывает `uvicorn.run(app, host="0.0.0.0", port=<panel_port>, ssl_*)`. Порт — `settings.ssl.panel_port` (по умолчанию 5000).
- **Lifespan**: современный ASGI context manager вместо устаревшего `@app.on_event` — `app.py:2179-2209` (`_app_lifespan`), привязан через `app.router.lifespan_context = _app_lifespan` (`app.py:2209`). Startup вызывает `_startup()` (`app.py:1775`); shutdown отменяет фоновые задачи, останавливает туннели и Telegram-бота.
- **Ключевые зависимости** (`requirements.txt`): `fastapi==0.115.12`, `starlette==0.46.2`, `uvicorn==0.34.0`, `pydantic==2.12.5`, `paramiko==3.5.1` (SSH), `cryptography==44.0.0` (Fernet), `slowapi==0.1.9` (rate-limit), `python-telegram-bot==20.7` (заявлен, но не используется — см. §10), `python-multipart==0.0.20`, `multicolorcaptcha==1.2.0`, `python-dotenv==1.2.2`, `httpx==0.25.2`, `PyYAML`, `bcrypt==5.0.0` (заявлен, но панель использует `hashlib.pbkdf2_hmac` — см. §5), `Flask==3.1.0` (legacy/не используется).

## 2. Конфигурация (`config.py`, 151 строка)

Единый источник env-настроек; `import config` триггерит `load_dotenv()` (`config.py:18-25`), поэтому должен импортироваться первым.

### Переменные окружения

| Env var | По умолчанию | Назначение | Где |
|---|---|---|---|
| `AWP_DEV` | `False` | Флаг dev-режима (truthy-парсер принимает `1/true/yes/on`). | `config.py:43` |
| `SECRET_KEY` | *(нет)* | Подпись session-кук (Starlette `SessionMiddleware`). **Обязателен, min 32 символа.** | `config.py:48-76` |
| `MASTER_KEY` | *(нет)* | Fernet-ключ шифрования SSH-секретов. **Обязателен, валидный Fernet-ключ.** | `config.py:83-112` |
| `APP_PORT` | `5000` | Порт контейнера/хоста. | `config.py:118` |
| `TRUSTED_HOSTS` | `localhost,127.0.0.1` | CSV разрешённых `Host` для `TrustedHostMiddleware`. | `config.py:120` |
| `SESSION_COOKIE_SECURE` | `False` | Флаг `Secure` (ставить `true` за HTTPS). | `config.py:125` |
| `SESSION_COOKIE_SAMESITE` | `lax` | Политика `SameSite`; валидируется в `lax\|strict\|none`. | `config.py:126-128` |
| `LOGIN_RATE_LIMIT` | `5/minute` | Строка лимита slowapi для логина. | `config.py:143` |
| `SHARE_RATE_LIMIT` | `10/minute` | Лимит share-эндпоинтов. | `config.py:144` |
| `CAPTCHA_RATE_LIMIT` | `30/minute` | Лимит captcha. | `config.py:145` |
| `BACKUP_INTERVAL_HOURS` | `6` | Период авто-бэкапа `data.json`; `0` отключает. | `config.py:150` |
| `BACKUP_KEEP_COUNT` | `14` | Сколько бэкапов хранить. | `config.py:151` |

### Fail-fast поведение

- Без `AWP_DEV`: отсутствие/короткость `SECRET_KEY` → `sys.exit(1)` (`config.py:59-76`).
- Отсутствие/невалидность `MASTER_KEY` → `sys.exit(1)` (`config.py:97-112`); формат проверяется мгновенно конструкцией `Fernet(MASTER_KEY.encode())`.
- `_rate_limit_key()` (`config.py:135-141`) извлекает клиентский IP для slowapi, предпочитая `X-Forwarded-For` (proxy/tunnel-aware), фолбэк — `request.client.host`.

### `AWP_DEV=1`

- `SECRET_KEY`: синтезируется **эфемерный** `secrets.token_urlsafe(48)` (сессии не переживут рестарт) (`config.py:52-57`).
- `MASTER_KEY`: синтезируется **эфемерный** `Fernet.generate_key()` (`config.py:85-96`). В обоих случаях выдаётся warning.
- Также расслабляет валидацию entrypoint (`docker-entrypoint.sh:6-8`) и пропускает `TrustedHostMiddleware` (`app.py:141`).

## 3. Модель данных (SQLAlchemy ORM)

Данные хранятся в реляционной БД через SQLAlchemy (async). Схема определена в `database/models.py`. Production: PostgreSQL через `asyncpg`; Dev: SQLite через `aiosqlite`. Конфигурация подключения — `config.DATABASE_URL`. Сессии — `database/session.py` (`get_db` dependency, `get_db_context` context manager).

`data.json` — **legacy**, больше не является первичным хранилищем. Функции `load_data`/`save_data`/`DATA_LOCK` удалены. Бэкапы через `routers/settings.py` теперь работают с JSON-экспортом из БД.

### ORM-модели (`database/models.py`)

**`User`** (таблица `users`):
- `id` (UUID, PK), `username` (unique, index), `password_hash` (PBKDF2, см. §5), `role` (`admin`|`support`|`user`), `enabled`, `created_at`
- Шеринг: `share_enabled`, `share_token`, `share_password_hash`
- Трафик: `traffic_reset_strategy`, `traffic_used` (BigInteger), `traffic_total` (BigInteger), `last_reset_at`, `expiration_date`, `traffic_limit`
- Опционально: `telegramId` (BigInteger), `email`, `description`, `remnawave_uuid`
- Relationships: `connections` → `UserConnection`, `api_tokens` → `ApiToken` (cascade delete)

**`Server`** (таблица `servers`):
- `id` (int, PK), `display_order`, `name`, `host`, `ssh_port` (default 22), `username`
- `password` *(v1: Fernet, см. §8)*, `private_key` *(v1: Fernet)*
- `server_info`, `host_fingerprint` (SHA256 SSH-публичного ключа)
- Relationships: `protocols` → `ServerProtocol`, `connections` → `UserConnection` (cascade delete)

**`ServerProtocol`** (таблица `server_protocols`):
- `id` (int, PK autoincrement), `server_id` (FK → servers, cascade)
- `protocol_type` (str, ключ вида `awg2`, `xray__2`)
- `installed`, `port`, `base_protocol`, `instance`, `display_name`, `container_name`
- `extra_params` (JSON): `{"awg_params": {...}, "settings": {...}}` — протоколо-специфичные данные (AWG-параметры, AdGuard mode/web_port, Nginx domain/email)

**`UserConnection`** (таблица `user_connections`):
- `id` (UUID, PK), `user_id` (FK → users, cascade), `server_id` (FK → servers, cascade)
- `protocol`, `client_id` (протоколо-специфичный), `name`, `created_at`, `last_bytes` (BigInteger)
- Relationships: `user` → User, `server` → Server

**`ApiToken`** (таблица `api_tokens`):
- `id` (UUID, PK), `name`, `token_hash` (SHA-256, никогда сырой), `token_prefix`
- `user_id` (FK → users, cascade), `created_at`, `last_used_at`
- Relationship: `user` → User

**`Setting`** (таблица `settings`, key-value):
- `key` (str, PK — например `"ssl"`, `"appearance"`, `"telegram"`)
- `value` (JSON)
- CRUD через `database/crud.py`: `get_setting(db, key, default)`, `set_setting(db, key, value)`

### Репозитории (`database/repositories.py`, 288 строк)

Содержат высокоуровневые операции над моделями: `UserRepository` (ensure_default_admin, CRUD пользователей), `ServerRepository` (CRUD серверов) и др.

### Шифрование at rest

- **Fernet** применяется только к `Server.password` и `Server.private_key` (см. §8). Формат хранения — `"v1:<fernet-token>"`.
- Хэши паролей пользователей — PBKDF2 (не Fernet) — см. §5.
- Секреты API-токенов — **SHA-256 хэш** (односторонний), не шифруются.
- Бэкапы делаются через JSON-экспорт из БД, шифрованные поля остаются в `v1:` формате — никогда не plaintext.

### Стартовые миграции (`_startup`)

`_startup()` в `app.py` вызывает `_maybe_migrate_legacy_data_file()` (one-time rescue старого `data.json` → volume-backed путь), затем через `database/repositories.py` создаёт дефолтного `admin/admin`, инициализирует настройки, запускает фоновые задачи и Telegram-бота.

### Переход от `data.json` к БД

- `get_ssh(server, db)` — поддерживает и dict (legacy), и ORM-модель Server.
- `_check_admin(db, request)` — теперь принимает `AsyncSession`.
- `get_panel_local_url(db, request)` / `get_panel_tunnel_target_url(db)` — читают SSL-настройки через `get_setting(db, 'ssl')`.
- Роутеры используют `Depends(get_db)` для получения сессии БД.

## 4. Цепочка middleware

`middleware.py` (111 строк) содержит два `BaseHTTPMiddleware`; остальные — в `app.py`.

**Порядок добавления (innermost → outermost), `app.py:105-142`:**

```
add SessionMiddleware            # app.py:105-110  (innermost)
add CsrfCookieMiddleware         # app.py:131
add SlowAPIMiddleware            # app.py:132
add SecurityHeadersMiddleware    # app.py:133
add TrustedHostMiddleware        # app.py:141-142  (outermost; в dev пропускается)
```

Комментарий `app.py:129-130` фиксирует эффективный порядок запроса: **TrustedHost → SecurityHeaders → SlowAPI → CSRF → Session**.

### `SessionMiddleware` (`app.py:105-110`)
- `secret_key=config.SECRET_KEY` (валидируется при загрузке).
- `https_only=config.SESSION_COOKIE_SECURE` (флаг `Secure`).
- `same_site=config.SESSION_COOKIE_SAMESITE`.
- HttpOnly неявно True (Starlette-дефолт).

### `CsrfCookieMiddleware` (`middleware.py:57-97`) — double-submit CSRF
- На каждый ответ без токена выставляет **не-HttpOnly** `csrf_token`-куку (`middleware.py:30`), чтобы JS мог её прочитать; samesite/secure зеркалят session-куку, `httponly=False` (`middleware.py:89-96`).
- Небезопасные методы (`POST/PUT/PATCH/DELETE`; безопасный набор `middleware.py:19`) на не-exempt путях отбиваются **403**, если заголовок `X-CSRF-Token` не совпадает с кукой через `hmac.compare_digest` (`middleware.py:76-85`).
- **Bearer-запросы обходят CSRF** (`middleware.py:48-50, 76-80`) — API-токены не привязаны к браузеру.
- Exempt-префиксы (`middleware.py:23-28`): `/api/auth/login`, `/api/auth/captcha`, `/share/`, `/api/share/`.

### `SecurityHeadersMiddleware` (`middleware.py:100-111`)
Добавляет: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-XSS-Protection: 0`, и CSP (`middleware.py:35-45`), разрешающий `'unsafe-inline'` для скриптов/стилей (нужно для Jinja), `connect-src 'self' https://api.github.com`, `frame-ancestors 'none'`.

### `SlowAPIMiddleware` + обработчик rate-limit
- `limiter = Limiter(key_func=config._rate_limit_key, default_limits=[])` (`app.py:48`).
- `app.state.limiter = limiter` и `app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)` (`app.py:136-137`) — отдаёт JSON 429.
- Лимиты на роуты через `@limiter.limit(...)` в роутерах: captcha, login, все `/api/share/*`.

### `TrustedHostMiddleware` (`app.py:141-142`)
- Добавляется **только при `not config.AWP_DEV` и непустом `TRUSTED_HOSTS`** — чтобы dev-доступ по LAN/IP не блокировался.

## 5. Модель аутентификации

### Сессионная аутентификация

- Login: `POST /api/auth/login` (`routers/auth.py`, `@limiter.limit(LOGIN_RATE_LIMIT)`).
  - Опциональная captcha (`settings.captcha.enabled`): ответ хранится в `request.session['captcha_answer']` на эндпоинте `/api/auth/captcha` (`routers/auth.py`), проверяется и стирается при логине.
  - При успехе: `request.session['user_id'] = u['id']`; возвращает `{status, role}`.
  - При неудаче: 401 с локализованной ошибкой.
- Logout: `GET /logout` очищает сессию (`routers/auth.py`, `request.session.clear()`).
- Текущий пользователь: `get_current_user(request)` (`app.py:1449-1457`) читает `request.session.get('user_id')`.

### Хэширование паролей (`app.py:1144-1156`)

- `hash_password(password)`: `salt = secrets.token_hex(16)`, `hashlib.pbkdf2_hmac('sha256', ..., 100000)`, хранится как `f"{salt}${h.hex()}"`.
- `verify_password(password, password_hash)`: сплит по `$`, повторная деривация, константное сравнение.
- Важно: `bcrypt` в `requirements.txt:3`, но **не используется** панелью; реальная схема — PBKDF2-SHA256, 100k итераций. Хранится в `users[].password_hash` и `users[].share_password_hash`.

### Роли и авторизация

- Три роли: `admin`, `support`, `user`.
- `_check_admin(db, request)` — центральный гейт, принимает `AsyncSession` (БД) и `Request`. Логика:
  1. Сессионный пользователь через `get_current_user`; если role ∈ `('admin','support')` → вернуть user.
  2. Иначе пробовать `Authorization: Bearer <token>`: резолв через `_resolve_api_token`; если владелец enabled и admin/support — вернуть его. Best-effort обновление `last_used_at`.
  3. Иначе `None` → вызывающий возвращает `403 Forbidden`.
- Роуты защищаются `if not await _check_admin(db, request): return JSONResponse({'error':'Forbidden'}, status_code=403)`.
- Эндпоинты обычных пользователей (`/api/my/*`) используют `get_current_user` напрямую и проверяют владение (`routers/users.py`).

### API-токены (`app.py:1086-1142`)

- Формат: `awp_<32 chars>` (`_generate_api_token`, `app.py:1097-1100`), ~256 бит.
- Хранится **только как SHA-256 дайджест** (`_hash_api_token`, `app.py:1091-1093`); сырое значение возвращается один раз при создании (`routers/api_tokens.py`).
- `_resolve_api_token` (`app.py:1103-1124`) матчит дайджест, затем проверяет, что владелец существует, enabled и admin/support — отключение/понижение владельца убивает токен.
- `_touch_api_token` (`app.py:1127-1141`) обновляет `last_used_at` не чаще раза в `API_TOKEN_TOUCH_INTERVAL = 300s` (`app.py:1088`).
- CSRF-middleware exempts bearer-auth (`middleware.py:48-50, 76-80`); проверено тестами (`tests/test_auth.py:102-109`).

### Публичный шеринг (отдельный flow)

- Каждый пользователь может иметь `share_token` (`secrets.token_urlsafe(16)`).
- `POST /api/share/{token}/auth` (`routers/sharing.py`) сверяет с `share_password_hash` (если задан) и ставит `request.session[f'share_auth_{token}'] = True`.
- `/share/{token}` и `/api/share/{token}/connections`, `/config/{id}` проверяют этот флаг.

## 6. Реестр менеджеров протоколов

### Диспетчер (`app.py:996-1020`)

`get_protocol_manager(ssh, protocol)` мапит `protocol_base(protocol)` → класс менеджера. Импорты **ленивые** (внутри функции). Фолбэк — `AWGManager(ssh)`.

| `base` | Класс | Конструктор | Модуль |
|---|---|---|---|
| `awg` / `awg2` / `awg_legacy` (дефолт) | `AWGManager` | `(ssh_manager)` | `managers/awg_manager.py` |
| `xray` | `XrayManager` | `(ssh_manager, protocol='xray')` | `managers/xray_manager.py:79,86` |
| `telemt` | `TelemtManager` | `(ssh_manager, protocol='telemt')` | `managers/telemt_manager.py` |
| `dns` | `DNSManager` | `(ssh)` | `managers/dns_manager.py` |
| `wireguard` | `WireGuardManager` | `(ssh_manager)` | `managers/wireguard_manager.py` |
| `socks5` | `Socks5Manager` | `(ssh, protocol='socks5')` | `managers/socks5_manager.py` |
| `adguard` | `AdguardManager` | `(ssh)` | `managers/adguard_manager.py` |
| `nginx` | `NginxManager` | `(ssh, protocol='nginx')` | `managers/nginx_manager.py` |

(`BackupManager(ssh)` — `managers/backup_manager.py`, инстанцируется напрямую в `routers/protocols.py`, минуя реестр.)

### Хелперы идентичности протоколов (`app.py:920-989`)

- `BASE_PROTOCOLS = ['awg','awg2','awg_legacy','xray','telemt','dns','wireguard','socks5','adguard','nginx']` (`app.py:920`).
- `MULTI_INSTANCE_PROTOCOLS = {'awg','awg2','awg_legacy','xray','telemt','socks5'}` (`app.py:921`) — только они могут иметь несколько инстансов.
- `protocol_base(p)` сплит по `__` (`app.py:924-925`); `protocol_instance(p)` парсит числовой суффикс (`app.py:928-935`); `protocol_key(base, idx)` строит `base__N` (`app.py:938-940`); `next_protocol_key` находит следующий свободный инстанс (`app.py:943-949`).
- `protocol_display_name` и `protocol_container_name` — мапы base→человекочитаемое имя / Docker-контейнер (`app.py:952-989`).

### Общий (duck-typed) интерфейс

Базового класса **нет**. Менеджеры реализуют общий контракт утиной типизацией. Каждый VPN-менеджер реализует (сигнатуры варьируются):

| Метод | AWG | Xray | Telemt | WireGuard | Socks5 | AdGuard | Nginx | DNS |
|---|---|---|---|---|---|---|---|---|
| `install_protocol` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `remove_container` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `get_server_status` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `check_docker_installed` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | *(inline)* |
| `check_protocol_installed` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | *(inline)* |
| `check_container_running` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | *(inline)* |
| `get_clients` | ✓ | ✓ | ✓ | ✓ | — | — | — | — |
| `add_client` | ✓ | ✓ | ✓ | ✓ | — | — | — | — |
| `remove_client` | ✓ | ✓ | ✓ | ✓ | — | — | — | — |
| `toggle_client` | ✓ | ✓ | ✓ | ✓ | — | — | — | — |
| `get_client_config` | ✓ | ✓ | ✓ | ✓ | — | — | — | — |
| `save_server_config` / `_get_server_config` | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | — |

### Расхождение сигнатуры WireGuard

`WireGuardManager`-методы **не принимают `protocol_type`** (одиночный инстанс). Хелпер `_manager_call(manager, method, protocol, *args)` (`app.py:1068-1073`) это нивелирует:

```python
def _manager_call(manager, method, protocol, *args, **kwargs):
    fn = getattr(manager, method)
    if isinstance(manager, WireGuardManager):
        return fn(*args, **kwargs)          # отбрасывает protocol
    return fn(protocol, *args, **kwargs)
```

### Удалённое подключение и зависимости

- **Все менеджеры общаются с удалённым сервером только через инжектированный `SSHManager`** (paramiko) — своих сокетов не открывают. Команды — `ssh.run_command` / `run_sudo_command` / `run_sudo_script`; файлы — `ssh.upload_file[_sudo]` / `download_file`.
- **Каждый протокол деплоится как Docker-контейнер** на удалённом хосте. Менеджер вызывает `check_docker_installed()` первым; `ensure_docker_installed` (`app.py:1024-1065`) ставит Docker при отсутствии.
- Константы контейнеров/образов:
  - AWG: `amnezia-awg`, `amnezia-awg-legacy`, `amnezia-awg2`.
  - Xray: `amnezia-xray`, образ `amneziavpn/amnezia-xray` (`xray_manager.py:83-84`).
  - WireGuard: `amnezia-wireguard`, образ `amneziavpn/amnezia-wg:latest` (`wireguard_manager.py:56-57`).
  - Telemt: контейнер `telemt`, API `http://127.0.0.1:9091` через `docker exec ... curl`.
  - SOCKS5: `amnezia-socks5proxy`, образ `3proxy/3proxy:0.9.5` (`socks5_manager.py:23-24`).
  - AdGuard: `amnezia-adguard`, образ `adguard/adguardhome:latest`, в сети `amnezia-dns-net`.
  - DNS: `amnezia-dns` на базе `mvance/unbound`, внутренняя сеть `172.29.172.0/24` (`dns_manager.py:45-49`).
  - Nginx: `amnezia-nginx` (`nginx:alpine`) + sidecar `amnezia-nginx-certbot` (`certbot/certbot`) для Let's Encrypt.
- Инстанс-суффиксация: multi-instance протоколы считают имена контейнеров/каталогов как `name-N` при `instance > 1` (`awg_manager.py:148-160`, `xray_manager.py:103-106`, `telemt_manager.py:32-35`).

## 7. SSH-слой (`managers/ssh_manager.py`, 285 строк)

- **Библиотека**: `paramiko` (`ssh_manager.py:6`). Без shell-out в `ssh` CLI.
- **Класс**: `SSHManager(host, port, username, password=None, private_key=None, host_fingerprint=None, on_fingerprint=None)` (`ssh_manager.py:56-66`).
- Context manager: `__enter__`/`__exit__` (`ssh_manager.py:280-285`).

### Политика host-key — **пиннинг по отпечатку** (НЕ `AutoAddPolicy`)

- `_PinningHostKeyPolicy(paramiko.MissingHostKeyPolicy)` (`ssh_manager.py:23-50`):
  - Если `expected` отпечаток задан и предложенный ключ отличается → `paramiko.SSHException` (защита от MITM/переустановки).
  - Если `expected` — `None` (первое подключение) → принимает и запоминает ключ.
  - OpenSSH-стиль `SHA256:...` отпечатков через `_sha256_fingerprint` (`ssh_manager.py:17-20`).
- **Отпечаток пинится только после успешного handshake** (`ssh_manager.py:104-113`): легитимный предыдущий пин не перезаписывается при более поздней неудаче (DNS/auth/timeout). Колбэк `on_fingerprint` сохраняет пин через лямбду в `get_ssh` (`app.py:261-264`).

### Аутентификация

- `allow_agent=False`, `look_for_keys=False` (`ssh_manager.py:79-80`) — только явные креды.
- Парсинг приватного ключа по порядку: `RSAKey` → `Ed25519Key` → `ECDSAKey` → `DSSKey` (`ssh_manager.py:85-98`). DSA — последний фолбэк.
- Пароль — только при отсутствии приватного ключа (`ssh_manager.py:99-100`).
- `_is_root = (username == 'root')` (`ssh_manager.py:66`) — короткое замыкание sudo.

### Выполнение команд

- `run_command(command, timeout=60, stdin_data=None)` (`ssh_manager.py:122-160`): `exec_command`, опционально пишет `stdin_data` в канал (для sudo-пароля), возвращает `(out, err, exit_code)` в utf-8 (`errors='replace'`).
- `run_sudo_command(command, timeout=60)` (`ssh_manager.py:162-182`): убирает существующий `sudo ` префикс; если root — напрямую; если есть пароль — `sudo -S -p '' <cmd>` с паролем через **stdin** (никогда в cmdline/`/proc/<pid>/cmdline`); иначе passwordless sudo.
- `run_sudo_script(script, timeout=120)` (`ssh_manager.py:184-202`): SFTP скрипта в `/tmp/_amnz_script_<md5>.sh`, запуск `sudo -S -p '' bash <tmp>; rm -f <tmp>` с паролем через stdin.
- `run_script(script, timeout=120)` (`ssh_manager.py:204-206`) — обычное выполнение.

### Передача файлов

- `upload_file(content, remote_path)` (`ssh_manager.py:208-221`): SFTP write; нормализует CRLF→LF.
- `upload_file_sudo(content, remote_path)` (`ssh_manager.py:223-243`): SFTP в `/tmp/_amnz_<md5>`, затем `sudo mv` (пути через `shlex.quote`) и `chmod 644`. `shlex.quote` на `remote_path` (`ssh_manager.py:241`) — митигация shell-инъекций.
- `download_file`, `file_exists`, `write_file` (alias для `upload_file_sudo`), `test_connection` (`ssh_manager.py:245-278`).

### Security-постура (уже укреплена)

- Нет `AutoAddPolicy`. Пиннинг обязателен.
- Sudo-пароли не появляются в `cmdline`/temp-файлах — подаются через SSH-канал stdin.
- Пути квотируются `shlex.quote`.
- **Дисциплина для новых менеджеров**: многие `run_sudo_command` строят команды через f-строки со значениями из `data.json`. Числовые значения безопасны; **строки из пользовательского ввода (domain/email для nginx, username для socks5) валидируются в менеджере** (`nginx_manager._validate_domain`/`_validate_email`, `nginx_manager.py:96-108`) — новый менеджер должен следовать тому же правилу validate-before-interpolate.

## 8. Слой секретов (`managers/secrets.py`, 88 строк)

- **Алгоритм**: Fernet (симметричный, `cryptography.fernet`).
- **Источник ключа**: `config.MASTER_KEY` (env; валидируется при загрузке в `config.py`). Закэшированный Fernet через `_get_fernet()` (`secrets.py:32-41`).
- **Версионируемый формат шифртекста**: `"v1:<fernet-token>"` (`_PREFIX = "v1:"`, `secrets.py:24, 57`). Префикс `v1:` отличает зашифрованное от legacy plaintext.
- **Публичный API**:
  - `encrypt_secret(plaintext)` (`secrets.py:44-57`): пусто → `""`; уже `v1:` → как есть (идемпотентно); иначе `v1:<token>`.
  - `decrypt_secret(value)` (`secrets.py:60-83`): пусто → `""`; не `v1:` → без изменений (legacy plaintext passthrough); `v1:` → расшифровка; плохой токен → `SecretError` (ловит `InvalidToken` + `UnicodeDecodeError`).
  - `is_encrypted(value)` (`secrets.py:86-88`): True, если начинается с `v1:`.
- **Где хранятся зашифрованные поля**: только `servers[].password` и `servers[].private_key`. Запись: `routers/servers.py`; чтение: `app.py:258-259`.
- **Миграция**: `scripts/migrate_secrets.py` (115 строк) обходит каждый сервер, шифрует `password`/`private_key` без `v1:`, пишет атомарно (`os.replace`) после бэкапа. Отказывается работать с `AWP_DEV=1` без `--force` (`migrate_secrets.py:104-114`).
- **Потеря ключа = потеря секретов**; описано в docstring модуля (`secrets.py:8-9`) и `.env.example:13-15`.

## 9. Обзор API-маршрутизации и роутеров (`routers/`)

В рамках декомпозиции монолита `app.py` конечные точки перенесены в модульный пакет `routers/` с использованием `fastapi.APIRouter`. Для **100% обратной совместимости** `app.py` монтирует роутеры через `app.include_router(...)` и ре-экспортирует функции хэндлеров и Pydantic-модели (как псевдонимы), что гарантирует работоспособность внешних вызовов, тестов и Telegram-бота без каких-либо изменений.

### Структура пакета `routers/`

| Роутер | Модуль | Описание и конечные точки |
|---|---|---|
| **Аутентификация** | `routers/auth.py` | `GET /api/auth/captcha`, `POST /api/auth/login` (rate-limited, капча, сессии). |
| **Шаблоны** | `routers/templates.py` | HTML-страницы Jinja2 (`/login`, `/set_lang/{lang}`, `/logout`, `/`, `/server/{id}`, `/users`, `/my`). |
| **Серверы** | `routers/servers.py` | Управление SSH-серверами (`/api/servers/add`, `/edit`, `/ping`, `/reset-fingerprint`, `/reorder`, `/delete`, `/reboot`, `/clear`, `/stats`, `/check`). |
| **Протоколы** | `routers/protocols.py` (523 строки) | Установка протоколов (`/api/servers/{id}/install` → `api_install_protocol` на строке 65, `/uninstall`), управление бэкапами, Docker-контейнерами, SOCKS5, Nginx. |
| **Подключения** | `routers/connections.py` | Управление VPN-клиентами (`/api/servers/{id}/connections/...` — GET, add, remove, edit, config, toggle). |
| **Пользователи** | `routers/users.py` | Пользователи панелей (`/api/users/...` — GET, add, update, delete, toggle, user connections, self-service `/api/my/connections`). |
| **Шеринг** | `routers/sharing.py` | Публичные ссылки доступа (`/share/{token}`, `/api/share/{token}/...` — auth, connections, config). |
| **Настройки** | `routers/settings.py` | Настройки панели (`/settings`, `/api/settings/...`), управление туннелями (cloudflare/ngrok), Cloudflare WARP, Telegram-ботом, синхронизация с Remnawave, бэкап/восстановление `data.json`. |
| **API-токены** | `routers/api_tokens.py` | Выпуск и аннулирование Bearer-токенов (`/api/settings/tokens/...`). |

### Паттерн взаимодействия роутеров и `app.py`
- Роутеры импортируют `app` как `import app as app_module` для использования общих функций и состояния (`load_data`, `save_data`, `get_ssh`, `_check_admin`, `limiter`, `DATA_LOCK`).
- `app.py` импортирует роутеры из `routers` и регистрирует их на экземпляр `app`:
  ```python
  from routers import auth, templates as templates_router, servers as servers_router, protocols as protocols_router, connections as connections_router, users as users_router, sharing as sharing_router, settings as settings_router, api_tokens as api_tokens_router
  app.include_router(auth.router)
  ...
  ```
  (`app.py:2029-2039`)
- Для сохранения полного контракта вызовов `app.py` объявляет псевдонимы хэндлеров и Pydantic-моделей:
  ```python
  LoginRequest = auth.LoginRequest
  api_login = auth.api_login
  ```

Порядок тегов OpenAPI кураторски задан в `OPENAPI_TAGS` (`app.py:68-79`) и драйвит `/docs` и `/redoc`.

## 10. Telegram-бот (`telegram_bot.py`, 1680 строк)

### Жизненный цикл

- Импортирован `app.py` как `import telegram_bot as tg_bot`.
- **Запускается in-process как asyncio-таск**, не отдельным процессом. Работает с БД через инжектированные колбэки.
- `launch_bot(...)` — `asyncio.create_task(_run_bot(...), name="telegram_bot")`.
- Старт из `_startup()` при `settings.telegram.enabled` и наличии токена.
- `stop_bot()` отменяет таск; вызывается из lifespan shutdown.

### Архитектура (без фреймворка `python-telegram-bot`)

Несмотря на `python-telegram-bot==20.7` в requirements, бот использует **самописный long-polling** поверх `httpx`:
- `TelegramAPI` (`telegram_bot.py:62-`) оборачивает `https://api.telegram.org/bot<token>/<method>` через `httpx.AsyncClient`; методы `call`, `get_updates` (long-poll, `allowed_updates=["message","callback_query"]`), `send_message`, `edit_message`.
- `_run_bot` (`telegram_bot.py:1188`) крутит poll-loop; `_dispatch` (`telegram_bot.py:1222`) маршрутизирует апдейты.
- Состояние передаётся **dependency injection** через колбэки — бот читает/пишет данные через БД.

### Возможности

- **Self-service**: `/start`, `/refresh`, `/get_config` — список подключений, конфиг VPN.
- **Admin-команды и inline-клавиатуры**: добавить сервер, просмотр серверов/пользователей/протоколов/клиентов, toggle контейнера, toggle/удаление/добавление клиента с привязкой к пользователю панели.
- **Admin-гвард**: `_require_admin(load_data_fn, tg_id)` (`telegram_bot.py:580`) матчит Telegram ID с `users[].telegramId` и ролью admin/support.
- **Mini-протокол callback_data**: `_ref(action, payload)` / `_resolve_ref(data_str)` (`telegram_bot.py:169-184`).
- **Переиспользование менеджеров**: `_get_ssh_and_manager` (`telegram_bot.py:348`) и `_manager_call` (`telegram_bot.py:387`) зеркалят хелперы app — бот драйвит **те же классы менеджеров** (включая special-case WireGuard).

## 11. Туннели и деплой

### `Dockerfile` (28 строк)

- `FROM python:3.14-slim` (`Dockerfile:3`).
- Non-root юзер `panel` (uid 1000) (`Dockerfile:6`); `USER panel` (`Dockerfile:23`).
- `PYTHONDONTWRITEBYTECODE=1` (`Dockerfile:11`).
- `EXPOSE 5000`, `ENTRYPOINT ["/app/docker-entrypoint.sh"]`, `CMD ["python3", "app.py"]`.

### `docker-compose.yml` (43 строки)

- Образ `prvtpro/amnezia-panel:latest`; порт `${APP_PORT:-5000}:5000`.
- `env_file: .env`; env `DATA_FILE=/app/data/data.json`, `TUNNEL_STATE_FILE=/app/data/tunnels_state.json`, `BACKUP_DIR=/app/data/backups`.
- Named volume `amnezia_data` → `/app/data`.
- **Закалён**: `read_only: true` rootfs, `tmpfs: /tmp:exec,size=64M`, `security_opt: no-new-privileges:true`, `cap_drop: ALL`.
- Healthcheck: TCP-коннект к localhost:5000 через inline python.

### `docker-entrypoint.sh` (24 строки)

- `set -e`; при `AWP_DEV=1` → `exec "$@"` немедленно (`docker-entrypoint.sh:6-8`).
- Иначе fail на отсутствии `SECRET_KEY` (`docker-entrypoint.sh:10-15`) или `MASTER_KEY` (`docker-entrypoint.sh:17-22`).

### Публичный туннель (in-process, `app.py`)

- Два провайдера в `TUNNEL_RUNTIMES = {'cloudflare': TunnelRuntime(), 'ngrok': TunnelRuntime()}` (`app.py:173-178`), под `TUNNEL_LOCK = threading.Lock()` (`app.py:179`).
- `TunnelRuntime` хранит `process`, `pid`, `public_url`, `started_at`, `output`.
- Функции: `find_tunnel_binary` (`app.py:430`), `install_tunnel_binary` (`app.py:672`), `build_tunnel_command` (`app.py:754`), `start_tunnel` (`app.py:763`, spawn через `subprocess.Popen`; ngrok использует env `NGROK_AUTHTOKEN`), `stop_tunnel` (`app.py:806`), `get_tunnel_status` (`app.py:870`).
- Туннель форвардит на `get_panel_tunnel_target_url()` (`app.py:281`) = `http(s)://127.0.0.1:<panel_port>`.
- В репо вендорен `bin/cloudflared`; ngrok скачивается.
- Состояние туннелей персистится в `TUNNEL_STATE_FILE` (`tunnels_state.json`).
- Роуты в `routers/settings.py`: `/api/settings/tunnels/{provider}/install|start|stop`, `DELETE /{provider}`, `GET /status`.

### Вариант Cloudflare WARP (опционально)

- `Dockerfile.warp`: `python:3.14-slim-bookworm` + `cloudflare-warp` из `pkg.cloudflareclient.com`.
- `docker-compose.warp.yml`: override с `cap_add: NET_ADMIN, SYS_MODULE`, `devices: /dev/net/tun`, sysctl `net.ipv4.conf.all.src_valid_mark=1`. Запуск: `docker compose -f docker-compose.yml -f docker-compose.warp.yml up -d --build`.
- `docker-entrypoint-warp.sh`: стартует `warp-svc` в фоне перед `exec "$@"`.
- В панели WARP управляется через `warp-cli` (`app.py:161`): `/api/settings/warp/connect|disconnect` (`routers/settings.py`).

## 12. Тесты (`tests/`)

- **Фреймворк**: pytest ≥8,<9 + pytest-asyncio + httpx (`requirements-dev.txt`). `TestClient` из `fastapi.testclient`.
- **`conftest.py`** (49 строк):
  - Добавляет корень проекта в `sys.path` (`conftest.py:9`).
  - Ставит `AWP_DEV=1` **до** импорта `app` (`conftest.py:13`) — триггерит эфемерный синтез ключей, тестам не нужны реальные секреты.
  - Фикстура `data_file` (`conftest.py:16-30`): пишет пустой `data.json` в `tmp_path`, отдаёт путь. Изоляция достигается не через env, а через прямой патч модуля.
  - Фикстура `app_client` (`conftest.py:33-45`): `TestClient(_app.app, raise_server_exceptions=False)` как context manager (ASGI lifespan/startup выполняется). Патчит `_app.DATA_FILE = data_file`.

### Что тестируется

- **`test_auth.py`** (137 строк): логин успех/неверный-пароль/нет-пользователя; rate-limit → 429; CSRF-отказ без токена, приём с токеном, обход bearer, exempt логина; captcha; security headers.
- **`test_api_tokens.py`** (334 строки): lifecycle API-токенов (create/list/revoke), bearer-auth без CSRF и без session-cookie, owner-state enforcement (disabled/downgraded владелец → токен перестаёт работать), SHA-256 hashing, ветки `_resolve_api_token`.
- **`test_helpers.py`** (205 строк): pbkdf2 password round-trip (уникальные соли, malformed-hash fallback), vpn-link encoding, translation fallback, token-prefix/throttle, one-time legacy `data.json` migration (`_maybe_migrate_legacy_data_file`).
- **`test_share.py`** (30 строк): share-auth 404 на плохом токене, share-страница 404, rate-limit → 429.
- **`test_secrets.py`** (56 строк): Fernet round-trip для паролей и приватных ключей, legacy plaintext passthrough, пустые, идемпотентность `encrypt_secret`, детект `v1:`, `SecretError` на плохом токене.
- **`test_amnezia_config.py`** (379 строк): интеграционные тесты Amnezia-конфигураций.
- **`test_telegram_bot_ui.py`** (62 строки): тесты UI Telegram-бота.
- **`tests/managers/test_awg_keys.py`** (30 строк): проверки `generate_wg_keypair` и `generate_awg_params`.
- **`tests/managers/test_xray_config.py`** (148 строк): чистые проверки структуры `build_initial_server_json` (VLESS-Reality inbound, Stats/API inbound на loopback, routing, int-coercion порта, инстанс-нейминг).
- **`tests/managers/test_wireguard_keys.py`** (17 строк): проверки WireGuard-ключей.

### Стратегия моков

- **Моков SSH/paramiko нет.** Тесты покрывают только пути, не требующие живого сервера (auth, CSRF, rate limits, крипто секретов, генерация ключей, API-токены, структура Xray `server.json`). Интеграция против реального SSH не входит в набор. Coverage-порог в CI — 20% (5 SSH/Docker-менеджеров на 0%, требуют integration-test инфраструктуры).

## 13. Паттерн расширения — добавление нового протокола

Синтезировано из конвенций кодовой базы. Добавление гипотетического протокола `foo`:

### Шаг 1 — Регистрация идентичности (в `app.py`)

1. Добавить `'foo'` в `BASE_PROTOCOLS` (`app.py:920`). В `MULTI_INSTANCE_PROTOCOLS` (`app.py:921`) — **только если** нужны несколько инстансов.
2. Добавить отображаемое имя в `protocol_display_name` (`app.py:952-969`) и имя контейнера в `protocol_container_name` (`app.py:971-989`).
3. Добавить ветку в `get_protocol_manager(ssh, protocol)` (`app.py:996-1020`) с ленивым импортом `FooManager`. Дефолтный фолбэк на `AWGManager` оставить последним.

### Шаг 2 — Создание менеджера (`managers/foo_manager.py`)

Следовать duck-typed интерфейсу (без базового класса). Для VPN-протокола с клиентами:

- Константы класса: `PROTOCOL = 'foo'`, `CONTAINER_NAME = 'amnezia-foo'`, `IMAGE_NAME = '...:tag'`, пути конфигов.
- `__init__(self, ssh_manager, protocol='foo')`: хранить `self.ssh`, выводить `self.instance`/`self.container_name` через стандартные `__`-сплит хелперы (копировать из `xray_manager.py:94-106` или `telemt_manager.py:23-35`).
- **Статус**: `check_docker_installed()`, `check_protocol_installed(protocol_type)`, `check_container_running(protocol_type)`, `get_server_status(protocol_type)` → `{container_exists, container_running, port, protocol, base_protocol, instance, container_name}`.
- **Жизненный цикл**: `install_protocol(protocol_type, port=None, ...)` → `{'status':'success'|'error', ...}` (зеркало `socks5_manager.py:149-203`); `remove_container(protocol_type)` — stop+rm контейнера и каталога конфига.
- **Клиенты** (только VPN): `get_clients`, `add_client`, `remove_client`, `toggle_client`, `get_client_config`. **Все клиентские методы должны принимать `protocol_type` первым аргументом**, чтобы работал generic-диспетчер `_manager_call` — кроме single-instance протоколов (как WireGuard), тогда нужно расширить `isinstance`-ветку `_manager_call` (`app.py:1068-1073`); предпочесть multi-arg форму.
- **Удалённые операции**: только `self.ssh.run_command` / `run_sudo_command` / `run_sudo_script` / `upload_file[_sudo]` / `download_file`. Валидировать пользовательские строки (domain, email) перед интерполяцией в команды (см. `nginx_manager._validate_domain`/`_validate_email`, `nginx_manager.py:96-108`); использовать `shlex.quote` для путей.

### Шаг 3 — Путь установки (в `routers/protocols.py`)

В `api_install_protocol` (`routers/protocols.py:65`) добавить ветку `elif install_base == 'foo':` с вызовом `manager.install_protocol(...)`, расширить построение `proto_record` (`routers/protocols.py:140-158`) для протоколо-специфичных метаданных. При необходимости новых полей — расширить Pydantic-модель `InstallProtocolRequest` (`routers/protocols.py:17-35`).

### Шаг 4 — Существующие роуты наследуются автоматически

Поскольку роуты подключений используют `get_protocol_manager` + `_manager_call` (`routers/connections.py`), `add`/`remove`/`toggle`/`config`/`clients` работают без доп. проводки, **если менеджер реализует клиентские методы со стандартной сигнатурой**. Роут toggle контейнера (`api_container_toggle`, `routers/protocols.py`) работает автоматически (нужен только `protocol_container_name()`).

### Шаг 5 — Бэкапы (опционально)

Добавить ветку в `BackupManager._paths_for` (`backup_manager.py:40-82`) с путями хоста и контейнера. Роуты `/api/servers/{id}/backups*` покроют новый протокол без доп. изменений.

### Шаг 6 — Telegram-бот (опционально)

Бот диспатчит динамически через `_get_ssh_and_manager`/`_manager_call` (`telegram_bot.py:348-381`) — как только `get_protocol_manager` узнаёт `foo`, потоки browsing/toggle/add/remove/config бота работают. Обновить мапу отображаемых имён (`_protocol_display_name`, `telegram_bot.py:145`), если нужно дружелюбное имя.

### Шаг 7 — UI (templates/static)

Добавить протокол в dropdown установки и поля настроек в `templates/server.html` и соотв. JS в `static/js/`. Мапа `protocol_display_name` драйвит большинство лейблов.
