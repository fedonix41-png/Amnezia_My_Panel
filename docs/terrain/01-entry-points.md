# Входные точки и Middleware

> Связан: [INDEX](INDEX.md) | [02-core](02-core.md) | [03-infra](03-infrastructure.md)

## ASGI Lifecycle

| Точка | Файл:строка | Описание |
|---|---|---|
| `app = FastAPI(...)` | `app.py:100` | Экземпляр с lifespan |
| `_app_lifespan()` | `app.py:2170` | startup → yield → shutdown |
| `_startup()` | `app.py:1763` | БД init, default admin, настройки, Telegram, bg tasks |
| `__main__` | `app.py:2206` | `uvicorn.run()` + опциональный SSL |

### Startup (`_startup`, `:1763`)
1. `Base.metadata.create_all` — создание таблиц
2. `UserRepository.ensure_default_admin()` — admin/admin если пусто
3. Инициализация settings (appearance, sync, captcha, telegram, ssl)
4. Telegram bot auto-start (если `telegram.enabled`)
5. Запуск `_bg_traffic` (traffic sync) и `_bg_backup`

### Shutdown (`:2179–2201`)
1. Cancel `_bg_traffic`, `_bg_backup`
2. Stop tunnel processes (CF, ngrok)
3. `tg_bot.stop_bot()`
4. PostHog shutdown

## HTML-страницы

| Маршрут | Роутер:строка | Шаблон |
|---|---|---|
| `GET /login` | `routers/templates.py:14` | `login.html` |
| `GET /` | `routers/templates.py:45` | `index.html` |
| `GET /server/{id}` | `routers/templates.py:65` | `server.html` |
| `GET /users` | `routers/templates.py:84` | `users.html` |
| `GET /my` | `routers/templates.py:111` | `my_connections.html` |
| `GET /settings` | `routers/settings.py:74` | `settings.html` |
| `GET /share/{token}` | `routers/sharing.py:49` | `user_share.html` |
| `GET /logout` | `routers/templates.py:37` | Редирект |
| `GET /set_lang/{lang}` | `routers/templates.py:21` | Устанавливает cookie |

Рендеринг через `app.tpl()` (`:1507`) — инжектирует: `current_user`, `site_settings`, `lang`, `_()`, `translations_json`.

## REST API — сводка

| Роутер | Файл | Эндпоинтов | Назначение |
|---|---|---|---|
| auth | `routers/auth.py` (87 строк) | 2 | Логин, CAPTCHA |
| servers | `routers/servers.py` (501 строк) | 10 | CRUD, ping, reboot, stats |
| protocols | `routers/protocols.py` (572 строки) | 12 | Install/uninstall, конфиги, бэкапы |
| connections | `routers/connections.py` (349 строк) | 6 | CRUD клиентов, toggle |
| users | `routers/users.py` (409 строк) | 8 | Пользователи + назначения |
| sharing | `routers/sharing.py` (217 строк) | 6 | Share-ссылки, self-service |
| settings | `routers/settings.py` (364 строки) | 15 | Настройки, туннели, WARP, sync |
| api_tokens | `routers/api_tokens.py` (106 строк) | 3 | Bearer-токены |
| templates | `routers/templates.py` (133 строки) | 7 | HTML-страницы |

Регистрация: `app.py:2020–2030`, все без префиксов.

## Telegram-бот

| Компонент | Строка | Описание |
|---|---|---|
| `launch_bot()` | `telegram_bot.py:104` | `asyncio.Task` с long-polling |
| `_run_bot()` | `:1550` | Polling цикл (`getUpdates`, timeout=25с) |
| `_dispatch()` | `:1586` | Callback dispatcher |
| `TelegramAPI` | `:128` | httpx-обёртка над Telegram HTTP API |
| `_callback_refs` | `:90, 253` | In-memory кеш callback data (TTL 6ч, cleanup >500) |
| `_pending_inputs` | `:91` | Multi-step ввод по chat_id |
| `_get_ssh_and_manager()` | `:414` | SSH из бота (через `asyncio.to_thread`) |

Команды: `/start`, `/admin`, `/connections`, `/servers`, `/addserver`, `/cancel`.
Callback patterns: `r:<ref_key>` (через `_ref()`/`_resolve_ref()`), `cfg:<id>`, `qr:<id>`, `adm:*`, `srv:<id>`.

## Middleware (outer → inner)

| # | Middleware | Источник | Когда |
|---|---|---|---|
| 1 | `TrustedHostMiddleware` | starlette | Prod only (skip при `AWP_DEV=1`) |
| 2 | `SecurityHeadersMiddleware` | `middleware.py:126` | Всегда |
| 3 | `SlowAPIMiddleware` | slowapi | Rate limiting |
| 4 | `CsrfCookieMiddleware` | `middleware.py:86` | Double-submit cookie |
| 5 | `SessionMiddleware` | starlette | Cookie-сессии |
| 6 | `PostHogRequestContextMiddleware` | `middleware.py:57` | Если PostHog настроен |

### CSRF exemptions (`middleware.py:23–28`)
- Безопасные методы: GET, HEAD, OPTIONS, TRACE
- Bearer-запросы (API-токены)
- `/api/auth/login`, `/api/auth/captcha`, `/share/`, `/api/share/`

### Rate limits (`config.py`)
- Login: `5/min` | Share: `10/min` | CAPTCHA: `30/min`
- Key: `X-Forwarded-For[0]` → `client.host` → `127.0.0.1`

### Security headers (`middleware.py:132–137`)
- CSP: `connect-src 'self' https://api.github.com` (проверка обновлений)
- `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`
