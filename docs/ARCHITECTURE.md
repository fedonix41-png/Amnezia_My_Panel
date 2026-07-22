# Amnezia Web Panel — Architecture (SSOT)

> Single Source of Truth для разработчиков и LLM-агентов. Этот документ **загружается по требованию** (Grep/Read), а не автозагружается в каждую сессию — для стартовых правил и индекса см. `AGENTS.md` в корне репозитория.
>
> Все факты проверены по коду; ссылки в формате `file:line`. Если код расходится с этим документом — код прав, обновите документ.

## 1. Технологический стек и запуск

- **Язык**: Python **3.14** (`Dockerfile:3` — `FROM python:3.14-slim`).
- **Веб-фреймворк**: **FastAPI** (ASGI) поверх **Starlette**.
  - `app.py:29` — `from fastapi import FastAPI, Request, Query, UploadFile, File`
  - `app.py:81-88` — `app = FastAPI(title="Amnezia Web Panel", openapi_tags=OPENAPI_TAGS, redoc_url=None)`
  - `app.py:91-103` — кастомный `/redoc`, pinned на `redoc@2`, Google Fonts отключены.
- **Сервер приложений**: **uvicorn** (`requirements.txt:36`; `app.py:4186` — `uvicorn.run(...)`).
- **Путь запуска**:
  - `Dockerfile:27-28` — `ENTRYPOINT ["/app/docker-entrypoint.sh"]`, `CMD ["python3", "app.py"]`.
  - `docker-entrypoint.sh:1-24` — валидирует `SECRET_KEY`/`MASTER_KEY`, кроме `AWP_DEV=1`, затем `exec "$@"`.
  - `app.py:4148-4186` — `if __name__ == '__main__':` читает SSL-настройки из `data.json`, вызывает `uvicorn.run(app, host="0.0.0.0", port=<panel_port>, ssl_*)`. Порт — `settings.ssl.panel_port` (по умолчанию 5000).
- **Lifespan**: современный ASGI context manager вместо устаревшего `@app.on_event` — `app.py:4114-4145` (`_app_lifespan`), привязан через `app.router.lifespan_context = _app_lifespan` (`app.py:4145`). Startup вызывает `_startup()` (`app.py:1725`); shutdown отменяет фоновые задачи, останавливает туннели и Telegram-бота.
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
- Также расслабляет валидацию entrypoint (`docker-entrypoint.sh:6-8`) и пропускает `TrustedHostMiddleware` (`app.py:123-124`).

## 3. Модель данных `data.json`

### Слой загрузки/сохранения (`app.py`)

- Резолв `DATA_FILE`: `app.py:130-139` — учитывает `sys.frozen` (PyInstaller), иначе каталог скрипта; basename `data.json`. Переопределяется через env `DATA_FILE` (используется docker-compose: `DATA_FILE=/app/data/data.json`, и тестами). При апгрейде — если новый путь пуст, а старый (`_LEGACY_DATA_FILE`, `app.py:139`) существует и не пуст, `_maybe_migrate_legacy_data_file` (`app.py:1687`) копирует его one-time при старте, чтобы не потерять зашифрованные секреты.
- `DATA_LOCK = asyncio.Lock()` (`app.py:192`) — защита конкурентных записей.
- `load_data()` (`app.py:195-221`) — `json.load` + `setdefault` по каждому top-level ключу.
- `save_data(data)` (`app.py:224-226`) — синхронный `json.dump(..., indent=2, ensure_ascii=False)`.
- `save_data_async(data)` (`app.py:229-232`) — `asyncio.to_thread(save_data)` под `DATA_LOCK`.
- `get_ssh(server)` (`app.py:235-247`) — строит `SSHManager`, расшифровывая `password`/`private_key` через `decrypt_secret` (`app.py:240-241`).

### Top-level схема

```
{
  "servers": [ ... ],          # app.py:201
  "users": [ ... ],            # app.py:202
  "user_connections": [ ... ], # app.py:203
  "api_tokens": [ ... ],       # app.py:204
  "settings": { appearance, sync, ssl, captcha, telegram }  # app.py:205-220 (+ миграции)
}
```

**`servers[]`** (пример `data.json:2-55`, запись `app.py:2157-2163`):
- `name`, `host`, `ssh_port`, `username`
- `password` *(зашифрован, см. §8)*, `private_key` *(зашифрован)*
- `server_info` (строка ОС из `uname`), `host_fingerprint` (SHA256 SSH-публичного ключа)
- `protocols` — dict, ключ — строка протокола (`awg2`, `awg_legacy`, `xray__2`); значение: `{installed, port, base_protocol, instance, display_name, container_name, awg_params|mode|domain...}` (запись `app.py:2670-2688`).

**`users[]`** (`data.json:57-94`, дефолтный admin `app.py:1730-1737`):
- `id` (UUID), `username`, `password_hash` (PBKDF2, см. §5), `role` (`admin`|`support`|`user`), `enabled`, `created_at`
- Шеринг: `share_enabled`, `share_token`, `share_password_hash` (nullable)
- Трафик: `traffic_reset_strategy` (`never`|`daily`|...), `traffic_used`, `traffic_total`, `last_reset_at`, `expiration_date`
- Опционально: `telegramId`, `email`, `description`, `traffic_limit`, `remnawave_uuid`

**`user_connections[]`** (`data.json:95-116`, запись `app.py:3157-3165`):
- `id` (UUID), `user_id`, `server_id` (int-индекс в `servers`), `protocol`, `client_id` (протоколо-специфичный), `name`, `created_at`, `last_bytes`

**`api_tokens[]`** (запись `app.py:4029-4037`):
- `id` (UUID), `name`, `token_hash` (SHA-256, никогда сырой), `token_prefix`, `user_id`, `created_at`, `last_used_at`

**`settings`** (`data.json:118-149`):
- `appearance` `{title, logo, subtitle}`
- `sync` `{remnawave_url, remnawave_api_key, remnawave_sync, remnawave_sync_users, remnawave_create_conns, remnawave_server_id, remnawave_protocol}`
- `ssl` `{enabled, domain, cert_path, key_path, cert_text, key_text, panel_port}` (миграция `app.py:1779-1791`)
- `captcha` `{enabled}`
- `telegram` `{token, enabled}`

### Шифрование at rest

- **Fernet** применяется только к `servers[].password` и `servers[].private_key` (см. §8). Формат хранения — `"v1:<fernet-token>"`. Записи: `app.py:2159-2160` (добавление сервера), `app.py:2199-2200` (редактирование).
- Хэши паролей пользователей — PBKDF2 (не Fernet) — см. §5.
- Секреты API-токенов — **SHA-256 хэш** (односторонний), не шифруются — `app.py:1069-1072`.
- Бэкапы `data.json` наследуют шифрование (`app.py:1836-1867`) — никогда не plaintext.

### Стартовые миграции (`_startup`, `app.py:1725-1794`)

Первой строкой `_startup` вызывает `_maybe_migrate_legacy_data_file()` (`app.py:1687-1723`) — one-time rescue старого `data.json` из каталога приложения в volume-backed путь (без overwrite существующего файла). Затем: создаёт дефолтного `admin/admin`, если `users` пуст; бэкфиллит `share_*`, `traffic_*`, `last_reset_at`, `expiration_date`; инициализирует `api_tokens`; мигрирует блок SSL.

## 4. Цепочка middleware

`middleware.py` (111 строк) содержит два `BaseHTTPMiddleware`; остальные — в `app.py`.

**Порядок добавления (innermost → outermost), `app.py:104-124`:**

```
add SessionMiddleware            # app.py:104-109  (innermost)
add CsrfCookieMiddleware         # app.py:113
add SlowAPIMiddleware            # app.py:114
add SecurityHeadersMiddleware    # app.py:115
add TrustedHostMiddleware        # app.py:123-124  (outermost; в dev пропускается)
```

Комментарий `app.py:111-112` фиксирует эффективный порядок запроса: **TrustedHost → SecurityHeaders → SlowAPI → CSRF → Session**.

### `SessionMiddleware` (`app.py:104-109`)
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
- `app.state.limiter = limiter` и `app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)` (`app.py:118-119`) — отдаёт JSON 429.
- Лимиты на роуты через `@limiter.limit(...)`: captcha (`app.py:2063`), login (`app.py:2081`), все `/api/share/*` (`app.py:3635, 3610, 3626, 3649`).

### `TrustedHostMiddleware` (`app.py:123-124`)
- Добавляется **только при `not config.AWP_DEV` и непустом `TRUSTED_HOSTS`** — чтобы dev-доступ по LAN/IP не блокировался.

## 5. Модель аутентификации

### Сессионная аутентификация

- Login: `POST /api/auth/login` (`app.py:2080-2101`, `@limiter.limit(LOGIN_RATE_LIMIT)`).
  - Опциональная captcha (`settings.captcha.enabled`): ответ хранится в `request.session['captcha_answer']` на эндпоинте `/api/auth/captcha` (`app.py:2071`), проверяется и стирается при логине (`app.py:2086-2091`).
  - При успехе: `request.session['user_id'] = u['id']` (`app.py:2098`); возвращает `{status, role}`.
  - При неудаче: 401 с локализованной ошибкой.
- Logout: `GET /logout` очищает сессию (`app.py:1994-2001`, `request.session.clear()`).
- Текущий пользователь: `get_current_user(request)` (`app.py:1427-1435`) читает `request.session.get('user_id')`.

### Хэширование паролей (`app.py:1122-1134`)

- `hash_password(password)`: `salt = secrets.token_hex(16)`, `hashlib.pbkdf2_hmac('sha256', ..., 100000)`, хранится как `f"{salt}${h.hex()}"`.
- `verify_password(password, password_hash)`: сплит по `$`, повторная деривация, константное сравнение.
- Важно: `bcrypt` в `requirements.txt:3`, но **не используется** панелью; реальная схема — PBKDF2-SHA256, 100k итераций. Хранится в `users[].password_hash` и `users[].share_password_hash`.

### Роли и авторизация

- Три роли: `admin`, `support`, `user`.
- `_check_admin(request)` (`app.py:2106-2132`) — центральный гейт, ~50 вызовов. Логика:
  1. Сессионный пользователь через `get_current_user`; если role ∈ `('admin','support')` → вернуть user.
  2. Иначе пробовать `Authorization: Bearer <token>`: резолв через `_resolve_api_token` (`app.py:2120`); если владелец enabled и admin/support — вернуть его. Best-effort обновление `last_used_at`.
  3. Иначе `None` → вызывающий возвращает `403 Forbidden`.
- Роуты защищаются `if not _check_admin(request): return JSONResponse({'error':'Forbidden'}, status_code=403)`.
- Эндпоинты обычных пользователей (`/api/my/*`) используют `get_current_user` напрямую и проверяют владение (`app.py:3236-3250`).

### API-токены (`app.py:1063-1120`)

- Формат: `awp_<32 chars>` (`_generate_api_token`, `app.py:1075-1078`), ~256 бит.
- Хранится **только как SHA-256 дайджест** (`_hash_api_token`, `app.py:1069-1072`); сырое значение возвращается один раз при создании (`app.py:4043-4051`).
- `_resolve_api_token` (`app.py:1081-1102`) матчит дайджест, затем проверяет, что владелец существует, enabled и admin/support — отключение/понижение владельца убивает токен.
- `_touch_api_token` (`app.py:1105-1119`) обновляет `last_used_at` не чаще раза в `API_TOKEN_TOUCH_INTERVAL = 300s` (`app.py:1066`).
- CSRF-middleware exempts bearer-auth (`middleware.py:48-50, 76-80`); проверено тестами (`tests/test_auth.py:102-109`).

### Публичный шеринг (отдельный flow)

- Каждый пользователь может иметь `share_token` (`secrets.token_urlsafe(16)`).
- `POST /api/share/{token}/auth` (`app.py:3652-3666`) сверяет с `share_password_hash` (если задан) и ставит `request.session[f'share_auth_{token}'] = True`.
- `/share/{token}` и `/api/share/{token}/connections`, `/config/{id}` проверяют этот флаг (`app.py:3676-3700`).

## 6. Реестр менеджеров протоколов

### Диспетчер (`app.py:978-1002`)

`get_protocol_manager(ssh, protocol)` мапит `protocol_base(protocol)` → класс менеджера. Импорты **ленивые** (внутри функции). Фолбэк — `AWGManager(ssh)`.

| `base` | Класс | Конструктор | Модуль |
|---|---|---|---|
| `awg` / `awg2` / `awg_legacy` (дефолт) | `AWGManager` | `(ssh_manager)` | `managers/awg_manager.py:124,132` |
| `xray` | `XrayManager` | `(ssh_manager, protocol='xray')` | `managers/xray_manager.py:79,86` |
| `telemt` | `TelemtManager` | `(ssh_manager, protocol='telemt')` | `managers/telemt_manager.py:12,16` |
| `dns` | `DNSManager` | `(ssh)` | `managers/dns_manager.py:7,8` |
| `wireguard` | `WireGuardManager` | `(ssh_manager)` | `managers/wireguard_manager.py:52,63` |
| `socks5` | `Socks5Manager` | `(ssh, protocol='socks5')` | `managers/socks5_manager.py:21,31` |
| `adguard` | `AdguardManager` | `(ssh)` | `managers/adguard_manager.py:23,39` |
| `nginx` | `NginxManager` | `(ssh, protocol='nginx')` | `managers/nginx_manager.py:23,39` |

(`BackupManager(ssh)` — `managers/backup_manager.py:6,16`, инстанцируется напрямую в `app.py:2816`, минуя реестр.)

### Хелперы идентичности протоколов (`app.py:902-975`)

- `BASE_PROTOCOLS = ['awg','awg2','awg_legacy','xray','telemt','dns','wireguard','socks5','adguard','nginx']` (`app.py:902`).
- `MULTI_INSTANCE_PROTOCOLS = {'awg','awg2','awg_legacy','xray','telemt','socks5'}` (`app.py:903`) — только они могут иметь несколько инстансов.
- `protocol_base(p)` сплит по `__` (`app.py:906-907`); `protocol_instance(p)` парсит числовой суффикс (`app.py:910-917`); `protocol_key(base, idx)` строит `base__N` (`app.py:920-922`); `next_protocol_key` находит следующий свободный инстанс (`app.py:925-931`).
- `protocol_display_name` и `protocol_container_name` — мапы base→человекочитаемое имя / Docker-контейнер (`app.py:934-971`).

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

`WireGuardManager`-методы **не принимают `protocol_type`** (одиночный инстанс). Хелпер `_manager_call(manager, method, protocol, *args)` (`app.py:1050-1055`) это нивелирует:

```python
def _manager_call(manager, method, protocol, *args, **kwargs):
    fn = getattr(manager, method)
    if isinstance(manager, WireGuardManager):
        return fn(*args, **kwargs)          # отбрасывает protocol
    return fn(protocol, *args, **kwargs)
```

Используется в `app.py:1151, 3146, 3216, 3239` и др.

### Удалённое подключение и зависимости

- **Все менеджеры общаются с удалённым сервером только через инжектированный `SSHManager`** (paramiko) — своих сокетов не открывают. Команды — `ssh.run_command` / `run_sudo_command` / `run_sudo_script`; файлы — `ssh.upload_file[_sudo]` / `download_file`.
- **Каждый протокол деплоится как Docker-контейнер** на удалённом хосте. Менеджер вызывает `check_docker_installed()` первым; `ensure_docker_installed` (`app.py:1006-1047`) ставит Docker при отсутствии.
- Константы контейнеров/образов:
  - AWG: `amnezia-awg`, `amnezia-awg-legacy`, `amnezia-awg2` (`awg_manager.py:148-160`).
  - Xray: `amnezia-xray`, образ `amneziavpn/amnezia-xray` (`xray_manager.py:83-84`).
  - WireGuard: `amnezia-wireguard`, образ `amneziavpn/amnezia-wg:latest` (`wireguard_manager.py:56-57`).
  - Telemt: контейнер `telemt`, API `http://127.0.0.1:9091` через `docker exec ... curl` (`telemt_manager.py:13-14, 52-66`).
  - SOCKS5: `amnezia-socks5proxy`, образ `3proxy/3proxy:0.9.5` (`socks5_manager.py:23-24`).
  - AdGuard: `amnezia-adguard`, образ `adguard/adguardhome:latest`, в сети `amnezia-dns-net` (`adguard_manager.py:25-31`).
  - DNS: `amnezia-dns` на базе `mvance/unbound`, внутренняя сеть `172.29.172.0/24` (`dns_manager.py:45-49`).
  - Nginx: `amnezia-nginx` (`nginx:alpine`) + sidecar `amnezia-nginx-certbot` (`certbot/certbot`) для Let's Encrypt (`nginx_manager.py:25-28`).
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
- **Отпечаток пинится только после успешного handshake** (`ssh_manager.py:104-114`): легитимный предыдущий пин не перезаписывается при более поздней неудаче (DNS/auth/timeout). Колбэк `on_fingerprint` сохраняет пин через лямбду в `get_ssh` (`app.py:243-246`).

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
- **Где хранятся зашифрованные поля**: только `servers[].password` и `servers[].private_key`. Запись: `app.py:2159-2160`; чтение: `app.py:240-241`, `app.py:2199-2200`.
- **Миграция**: `scripts/migrate_secrets.py` (115 строк) обходит каждый сервер, шифрует `password`/`private_key` без `v1:`, пишет атомарно (`os.replace`) после бэкапа. Отказывается работать с `AWP_DEV=1` без `--force` (`migrate_secrets.py:104-114`).
- **Потеря ключа = потеря секретов**; описано в docstring модуля (`secrets.py:8-9`) и `.env.example:13-15`.

## 9. Обзор API-маршрутизации

Все роуты объявлены на едином `app` (без `APIRouter`). Группировка по тегу/префиксу:

### HTML-страницы (`tags=["System Templates"]`) — `app.py:1979-2066`
`GET /login`, `/set_lang/{lang}`, `/logout`, `/`, `/server/{server_id}`, `/users`, `/my`, `/settings`, `/share/{token}`.

### Аутентификация (`tags=["Authentication"]`) — `app.py:2062-2101`
`GET /api/auth/captcha` (rate-limited), `POST /api/auth/login` (rate-limited).

### Серверы (`tags=["Servers"]`) — `app.py:2135-2458`
`/api/servers/add`, `/{server_id}/edit`, `/ping`, `/reset-fingerprint`, `/reorder`, `/{server_id}/delete`, `/reboot`, `/clear`, `/stats`, `/check`.

### Протоколы (`tags=["Protocols"]`) — `app.py:2590-3061`
`/install`, `/socks5/credentials` (GET+POST), `/uninstall`, `/backups`, `/backups/create`, `/backups/download`, `/container/toggle`, `/server_config`, `/server_config/save`, `/nginx/site`, `/nginx/site/save`.

### Подключения (`tags=["Connections"]`) — `app.py:3084-3291`
`/api/servers/{server_id}/connections` (GET), `/add`, `/remove`, `/edit`, `/config`, `/toggle`. `GET /api/servers/{server_id}/{protocol}/clients` (`app.py:3953`).

### Пользователи (`tags=["Users"]`) — `app.py:3293-3597`
`/api/users` (GET), `/add`, `/{user_id}/update`, `/delete`, `/toggle`, `/{user_id}/connections/add`, `/{user_id}/connections` (GET).

### Self-service (`tags=["Self-service"]`) — `app.py:3597, 3682`
`GET /api/my/connections`, `POST /api/my/connections/{connection_id}/config`.

### Шеринг (публичный, token-protected) (`tags=["Sharing"]`) — `app.py:3652-3723`
`POST /api/share/{token}/auth`, `GET /api/share/{token}/connections`, `POST /api/share/{token}/config/{connection_id}`. Все rate-limited; CSRF-exempt.

### Настройки (`tags=["Settings"]`) — `app.py:3757-3952, 4027-4068`
`GET/POST /api/settings`, `/save`, `/tunnels/status`, `/tunnels/{provider}/install|start|stop`, `DELETE /tunnels/{provider}`, `/warp/connect|disconnect`, `/telegram/toggle`, `/sync_now`, `/sync_delete`, `/backup/download`, `/backup/restore`.

### API-токены (`tags=["API Tokens"]`) — `app.py:3985-4067`
`GET /api/settings/tokens`, `POST`, `DELETE /{token_id}`.

Порядок тегов OpenAPI кураторски задан в `OPENAPI_TAGS` (`app.py:68-79`) и драйвит `/docs` и `/redoc`.

## 10. Telegram-бот (`telegram_bot.py`, 1137 строк)

### Жизненный цикл

- Импортирован `app.py:61` как `import telegram_bot as tg_bot`.
- **Запускается in-process как asyncio-таск**, не отдельным процессом. Общий `data.json` через инжектированные колбэки.
- `launch_bot(token, load_data_fn, generate_vpn_link_fn, save_data_fn=None)` (`telegram_bot.py:38-44`) — `asyncio.create_task(_run_bot(...), name="telegram_bot")`.
- Старт из `_startup()` при `settings.telegram.enabled` и наличии токена (`app.py:1802-1806`).
- `stop_bot()` (`telegram_bot.py:47-56`) отменяет таск; вызывается из lifespan shutdown (`app.py:4140`).

### Архитектура (без фреймворка `python-telegram-bot`)

Несмотря на `python-telegram-bot==20.7` в requirements, бот использует **самописный long-polling** поверх `httpx`:
- `TelegramAPI` (`telegram_bot.py:62-`) оборачивает `https://api.telegram.org/bot<token>/<method>` через `httpx.AsyncClient`; методы `call`, `get_updates` (long-poll, `allowed_updates=["message","callback_query"]`), `send_message`, `edit_message`.
- `_run_bot` (`telegram_bot.py:1004`) крутит poll-loop; `_dispatch` (`telegram_bot.py:1038`) маршрутизирует апдейты.
- Состояние передаётся **dependency injection** `load_data_fn`, `generate_vpn_link_fn`, `save_data_fn` — бот читает/пишет тот же `data.json`.

### Возможности

- **Self-service**: `/start`, `/refresh`, `/get_config` — список подключений, конфиг VPN.
- **Admin-команды и inline-клавиатуры**: добавить сервер, просмотр серверов/пользователей/протоколов/клиентов, toggle контейнера, toggle/удаление/добавление клиента с привязкой к пользователю панели.
- **Admin-гвард**: `_require_admin(load_data_fn, tg_id)` (`telegram_bot.py:580`) матчит Telegram ID с `users[].telegramId` и ролью admin/support.
- **Mini-протокол callback_data**: `_ref(action, payload)` / `_resolve_ref(data_str)` (`telegram_bot.py:169-184`).
- **Переиспользование менеджеров**: `_get_ssh_and_manager` (`telegram_bot.py:335`) и `_manager_call` (`telegram_bot.py:374`) зеркалят хелперы app — бот драйвит **те же классы менеджеров** (включая special-case WireGuard).

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

- Два провайдера в `TUNNEL_RUNTIMES = {'cloudflare': TunnelRuntime(), 'ngrok': TunnelRuntime()}` (`app.py:155-158`), под `TUNNEL_LOCK = threading.Lock()` (`app.py:159`).
- `TunnelRuntime` хранит `process`, `pid`, `public_url`, `started_at`, `output`.
- Функции: `find_tunnel_binary`, `download_tunnel_binary`, `install_tunnel_binary`, `build_tunnel_command`, `start_tunnel(provider, local_url, authtoken='')` (`app.py:745-787`, spawn через `subprocess.Popen`; ngrok использует env `NGROK_AUTHTOKEN`), `stop_tunnel` (`app.py:788`), `get_tunnel_status` (`app.py:852`).
- Туннель форвардит на `get_panel_tunnel_target_url()` = `http(s)://127.0.0.1:<panel_port>` (`app.py:263-268`).
- В репо вендорен `bin/cloudflared`; ngrok скачивается.
- Состояние туннелей персистится в `TUNNEL_STATE_FILE` (`tunnels_state.json`).
- Роуты: `/api/settings/tunnels/{provider}/install|start|stop`, `DELETE /{provider}`, `GET /status` (`app.py:3774-3846`).

### Вариант Cloudflare WARP (опционально)

- `Dockerfile.warp`: `python:3.14-slim-bookworm` + `cloudflare-warp` из `pkg.cloudflareclient.com`.
- `docker-compose.warp.yml`: override с `cap_add: NET_ADMIN, SYS_MODULE`, `devices: /dev/net/tun`, sysctl `net.ipv4.conf.all.src_valid_mark=1`. Запуск: `docker compose -f docker-compose.yml -f docker-compose.warp.yml up -d --build`.
- `docker-entrypoint-warp.sh`: стартует `warp-svc` в фоне перед `exec "$@"`.
- В панели WARP управляется через `warp-cli` (`app.py:161`): `/api/settings/warp/connect|disconnect` (`app.py:3846-3867`).

## 12. Тесты (`tests/`)

- **Фреймворк**: pytest ≥8,<9 + pytest-asyncio + httpx (`requirements-dev.txt`). `TestClient` из `fastapi.testclient`.
- **`conftest.py`** (49 строк):
  - Добавляет корень проекта в `sys.path` (`conftest.py:9`).
  - Ставит `AWP_DEV=1` **до** импорта `app` (`conftest.py:13`) — триггерит эфемерный синтез ключей, тестам не нужны реальные секреты.
  - Фикстура `data_file` (`conftest.py:16-30`): пишет пустой `data.json` в `tmp_path`, отдаёт путь. Изоляция достигается не через env, а через прямой патч модуля (см. ниже).
  - Фикстура `app_client` (`conftest.py:33-45`): `TestClient(_app.app, raise_server_exceptions=False)` как context manager (ASGI lifespan/startup выполняется). Патчит `_app.DATA_FILE = data_file` — `app.py` биндит путь один раз при импорте, поэтому модуль-глобал (а не env) указывает на изолированный файл каждого теста.

### Что тестируется

- **`test_auth.py`** (137 строк): логин успех/неверный-пароль/нет-пользователя; rate-limit → 429; CSRF-отказ без токена, приём с токеном, обход bearer, exempt логина; captcha; security headers.
- **`test_api_tokens.py`** (334 строки): lifecycle API-токенов (create/list/revoke), bearer-auth без CSRF и без session-cookie, owner-state enforcement (disabled/downgraded владелец → токен перестаёт работать), SHA-256 hashing, ветки `_resolve_api_token`.
- **`test_helpers.py`** (181 строка): pbkdf2 password round-trip (уникальные соли, malformed-hash fallback), vpn-link encoding, translation fallback, token-prefix/throttle, one-time legacy `data.json` migration (`_maybe_migrate_legacy_data_file`).
- **`test_share.py`** (30 строк): share-auth 404 на плохом токене, share-страница 404, rate-limit → 429.
- **`test_secrets.py`** (56 строк): Fernet round-trip для паролей и приватных ключей, legacy plaintext passthrough, пустые, идемпотентность `encrypt_secret`, детект `v1:`, `SecretError` на плохом токене.
- **`tests/managers/test_awg_keys.py`** (30 строк): проверки `generate_wg_keypair` и `generate_awg_params`.
- **`tests/managers/test_xray_config.py`** (148 строк): чистые проверки структуры `build_initial_server_json` (VLESS-Reality inbound, Stats/API inbound на loopback, routing, int-coercion порта, инстанс-нейминг).

### Стратегия моков

- **Моков SSH/paramiko нет.** Тесты покрывают только пути, не требующие живого сервера (auth, CSRF, rate limits, крипто секретов, генерация ключей, API-токены, структура Xray `server.json`). Интеграция против реального SSH не входит в набор. Coverage-порог в CI — 20% (5 SSH/Docker-менеджеров на 0%, требуют integration-test инфраструктуры).

## 13. Паттерн расширения — добавление нового протокола

Синтезировано из конвенций кодовой базы. Добавление гипотетического протокола `foo`:

### Шаг 1 — Регистрация идентичности (в `app.py`)

1. Добавить `'foo'` в `BASE_PROTOCOLS` (`app.py:902`). В `MULTI_INSTANCE_PROTOCOLS` (`app.py:903`) — **только если** нужны несколько инстансов.
2. Добавить отображаемое имя в `protocol_display_name` (`app.py:937-948`) и имя контейнера в `protocol_container_name` (`app.py:956-967`).
3. Добавить ветку в `get_protocol_manager(ssh, protocol)` (`app.py:978-1002`) с ленивым импортом `FooManager`. Дефолтный фолбэк на `AWGManager` оставить последним.

### Шаг 2 — Создание менеджера (`managers/foo_manager.py`)

Следовать duck-typed интерфейсу (без базового класса). Для VPN-протокола с клиентами:

- Константы класса: `PROTOCOL = 'foo'`, `CONTAINER_NAME = 'amnezia-foo'`, `IMAGE_NAME = '...:tag'`, пути конфигов.
- `__init__(self, ssh_manager, protocol='foo')`: хранить `self.ssh`, выводить `self.instance`/`self.container_name` через стандартные `__`-сплит хелперы (копировать из `xray_manager.py:94-106` или `telemt_manager.py:23-35`).
- **Статус**: `check_docker_installed()`, `check_protocol_installed(protocol_type)`, `check_container_running(protocol_type)`, `get_server_status(protocol_type)` → `{container_exists, container_running, port, protocol, base_protocol, instance, container_name}`.
- **Жизненный цикл**: `install_protocol(protocol_type, port=None, ...)` → `{'status':'success'|'error', ...}` (зеркало `socks5_manager.py:149-203`); `remove_container(protocol_type)` — stop+rm контейнера и каталога конфига.
- **Клиенты** (только VPN): `get_clients`, `add_client`, `remove_client`, `toggle_client`, `get_client_config`. **Все клиентские методы должны принимать `protocol_type` первым аргументом**, чтобы работал generic-диспетчер `_manager_call` — кроме single-instance протоколов (как WireGuard), тогда нужно расширить `isinstance`-ветку `_manager_call` (`app.py:1050-1055`); предпочесть multi-arg форму.
- **Удалённые операции**: только `self.ssh.run_command` / `run_sudo_command` / `run_sudo_script` / `upload_file[_sudo]` / `download_file`. Валидировать пользовательские строки (domain, email) перед интерполяцией в команды (см. `nginx_manager._validate_domain`/`_validate_email`, `nginx_manager.py:96-108`); использовать `shlex.quote` для путей.

### Шаг 3 — Путь установки (в `app.py`)

В `api_install_protocol` (`app.py:2590-2698`) добавить ветку `elif install_base == 'foo':` с вызовом `manager.install_protocol(...)`, расширить построение `proto_record` (`app.py:2670-2688`) для протоколо-специфичных метаданных. При необходимости новых полей — расширить Pydantic-модель `InstallProtocolRequest`.

### Шаг 4 — Существующие роуты наследуются автоматически

Поскольку роуты подключений используют `get_protocol_manager` + `_manager_call` (`app.py:3123-3288`), `add`/`remove`/`toggle`/`config`/`clients` работают без доп. проводки, **если менеджер реализует клиентские методы со стандартной сигнатурой**. Роут toggle контейнера (`api_container_toggle`, `app.py:2917-2947`) работает автоматически (нужен только `protocol_container_name()`).

### Шаг 5 — Бэкапы (опционально)

Добавить ветку в `BackupManager._paths_for` (`backup_manager.py:40-82`) с путями хоста и контейнера. Роуты `/api/servers/{id}/backups*` покроют новый протокол без доп. изменений.

### Шаг 6 — Telegram-бот (опционально)

Бот диспатчит динамически через `_get_ssh_and_manager`/`_manager_call` (`telegram_bot.py:335-381`) — как только `get_protocol_manager` узнаёт `foo`, потоки browsing/toggle/add/remove/config бота работают. Обновить мапу отображаемых имён (`_protocol_display_name`, `telegram_bot.py:132`), если нужно дружелюбное имя.

### Шаг 7 — UI (templates/static)

Добавить протокол в dropdown установки и поля настроек в `templates/server.html` и соотв. JS в `static/js/`. Мапа `protocol_display_name` драйвит большинство лейблов.

---

## Быстрые якоря (file:line)

- Конструкция app: `app.py:81-88`. Порядок middleware: `app.py:104-124`. Lifespan: `app.py:4114-4145`. `uvicorn.run`: `app.py:4186`.
- Слой данных: `load_data` `app.py:195`, `save_data` `app.py:224`, `save_data_async` `app.py:229`, `get_ssh` `app.py:235`.
- Auth: `_check_admin` `app.py:2106`, `get_current_user` `app.py:1427`, login `app.py:2080`, `hash_password`/`verify_password` `app.py:1122`/`1124`.
- API-токены: `app.py:1063-1120`, роут создания `app.py:4009-4051`.
- Реестр протоколов: `BASE_PROTOCOLS` `app.py:902`, `get_protocol_manager` `app.py:978`, `_manager_call` `app.py:1050`, `ensure_docker_installed` `app.py:1006`.
- Секреты: `managers/secrets.py`. SSH: `managers/ssh_manager.py`.
- Туннели: `TUNNEL_RUNTIMES` `app.py:155`, `start_tunnel` `app.py:745`, `stop_tunnel` `app.py:788`.
- Telegram: `launch_bot` `telegram_bot.py:38`, `stop_bot` `telegram_bot.py:47`, `_run_bot` `telegram_bot.py:1004`.
