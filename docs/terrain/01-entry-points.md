# Входные точки и Middleware

> Связан: [INDEX](INDEX.md) | [02-core](02-core.md) | [03-infra](03-infrastructure.md)

Номера строк намеренно не используются: навигируй по именам функций, классов и секциям, чтобы документ не устаревал после форматирования.

## ASGI Lifecycle

| Точка | Расположение | Описание |
|---|---|---|
| `app = FastAPI(...)` | `app.py` | Экземпляр приложения с lifespan |
| `_app_lifespan()` | `app.py` | startup → yield → shutdown |
| `_startup()` | `app.py` | БД, default admin, настройки, Telegram, фоновые задачи |
| `__main__` | `app.py` | `uvicorn.run()` и опциональный SSL |

### Startup (`_startup()`)

1. `Base.metadata.create_all` — создание таблиц.
2. `UserRepository.ensure_default_admin()` — создаёт `admin/admin`, если пользователей нет.
3. Инициализация settings: appearance, sync, captcha, telegram, ssl.
4. Telegram bot auto-start при `telegram.enabled`.
5. Запуск `_bg_traffic` и `_bg_backup`.

### Shutdown (`_app_lifespan()`)

1. Отменяет `_bg_traffic` и `_bg_backup`.
2. Останавливает tunnel processes (CF, ngrok).
3. Вызывает `tg_bot.stop_bot()`.
4. Завершает PostHog.

## HTML-страницы

| Маршрут | Роутер | Шаблон |
|---|---|---|
| `GET /login` | `routers/templates.py` | `login.html` |
| `GET /` | `routers/templates.py` | `index.html` |
| `GET /server/{id}` | `routers/templates.py` | `server.html` |
| `GET /users` | `routers/templates.py` | `users.html` |
| `GET /my` | `routers/templates.py` | `my_connections.html` |
| `GET /settings` | `routers/settings.py` | `settings.html` |
| `GET /share/{token}` | `routers/sharing.py` | `user_share.html` |
| `GET /logout` | `routers/templates.py` | Редирект |
| `GET /set_lang/{lang}` | `routers/templates.py` | Устанавливает cookie |

Рендеринг через `app.tpl()` инжектирует `current_user`, `site_settings`, `lang`, `_()` и `translations_json`.

## REST API — сводка

| Роутер | Файл | Назначение |
|---|---|---|
| auth | `routers/auth.py` | Логин, CAPTCHA |
| servers | `routers/servers.py` | CRUD, ping, reboot, stats |
| protocols | `routers/protocols.py` | Install/uninstall, конфиги, бэкапы (list/create/download/upload/restore/delete) |
| awg_plugin | `routers/awg_plugin.py` | Admin API обновлений компонентов AWG (kernel module/userspace): status/changelog/update/cancel |
| connections | `routers/connections.py` | CRUD клиентов, toggle |
| users | `routers/users.py` | Пользователи и назначения |
| sharing | `routers/sharing.py` | Share-ссылки, self-service |
| settings | `routers/settings.py` | Настройки, туннели, WARP, sync |
| api_tokens | `routers/api_tokens.py` | Bearer-токены |
| templates | `routers/templates.py` | HTML-страницы |

Все роутеры регистрируются в секции `ROUTERS REGISTRATION` в `app.py` и не имеют префиксов.

Контрактные тесты опасных изменяющих маршрутов находятся в `tests/test_core_routes.py`. Они заменяют только внешние SSH/Docker-вызовы, но проверяют реальные HTTP-маршруты, формы вызова менеджеров и записи в БД для серверов, протоколов, подключений, пользователей и настроек.

## Telegram-бот

| Компонент | Расположение | Описание |
|---|---|---|
| `launch_bot()` | `telegram_bot.py` | `asyncio.Task` с long-polling |
| `_run_bot()` | `telegram_bot.py` | Polling цикл (`getUpdates`, timeout=25с) |
| `_dispatch()` | `telegram_bot.py` | Callback dispatcher |
| `TelegramAPI` | `telegram_bot.py` | httpx-обёртка над Telegram HTTP API |
| `_callback_refs` | `telegram_bot.py` | In-memory кеш callback data (TTL 6ч, cleanup >500) |
| `_pending_inputs` | `telegram_bot.py` | Multi-step ввод по chat_id |
| `_get_ssh_and_manager()` | `telegram_bot.py` | SSH из бота (через `asyncio.to_thread`) |

Команды: `/start`, `/admin`, `/connections`, `/servers`, `/addserver`, `/cancel`.
Callback patterns: `r:<ref_key>` (через `_ref()`/`_resolve_ref()`), `cfg:<id>`, `qr:<id>`, `adm:*`, `srv:<id>`.

## Middleware (outer → inner)

| # | Middleware | Источник | Когда |
|---|---|---|---|
| 1 | `TrustedHostMiddleware` | starlette | Только production; пропущен при `AWP_DEV=1` |
| 2 | `SecurityHeadersMiddleware` | `middleware.py` | Всегда |
| 3 | `SlowAPIMiddleware` | slowapi | Rate limiting |
| 4 | `CsrfCookieMiddleware` | `middleware.py` | Double-submit cookie |
| 5 | `SessionMiddleware` | starlette | Cookie-сессии |
| 6 | `PostHogRequestContextMiddleware` | `middleware.py` | Если PostHog настроен |

### CSRF exemptions

- Безопасные методы: GET, HEAD, OPTIONS, TRACE.
- Bearer-запросы (API-токены).
- `/api/auth/login`, `/api/auth/captcha`, `/share/`, `/api/share/`.

### Rate limits (`config.py`)

- Login: `5/min`, share: `10/min`, CAPTCHA: `30/min`.
- Key: `X-Forwarded-For[0]` → `client.host` → `127.0.0.1`.

### Security headers

- CSP: `connect-src 'self' https://api.github.com` (проверка обновлений).
- `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`.
