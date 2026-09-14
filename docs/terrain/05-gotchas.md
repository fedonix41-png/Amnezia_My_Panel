# Подводные камни, Глоссарий, Dev/Prod

> Связан: [INDEX](INDEX.md) | [02-core](02-core.md) | [03-infra](03-infrastructure.md)

## Глобальные мутабельные состояния

⚠️ Всё состояние хранится в БД. Исключения — процессные модульные глобалы, опасные при рефакторинге:

- `TUNNEL_RUNTIMES` в `app.py` — `dict[str, TunnelRuntime]` с `Popen`/PID, мутируется из watcher-потоков. Все мутации под `TUNNEL_LOCK`.
- `_callback_refs` и `_pending_inputs` в `telegram_bot.py` — обычные dict без `asyncio.Lock`. Безопасны при одном polling-потоке, но race возможен при добавлении конкурентных обработчиков.

Остальные глобалы (`posthog_client`, `TRANSLATIONS`, `DATA_FILE`) — read-only после startup, для агента не критичны.

## Магические числа

Только те, что легко случайно нарушить при рефакторинге:

| Значение | Где | Почему важно |
|---|---|---|
| `600_000` / `100_000` | `app.py` / `database/repositories.py` | PBKDF2 итерации — расходятся, менять согласованно с `verify_password` |
| `300` (5 мин) | `API_TOKEN_TOUCH_INTERVAL` в `app.py` | Троттлинг перезаписи `last_used` |
| `25` (сек) | `telegram_bot.py` | Long-polling timeout |
| `15` (сек) | `managers/ssh_manager.py` | SSH connect timeout |

## WireGuardManager — исключение в сигнатуре

См. [02-core §Диспетчеризация](02-core.md) — единственный источник истины. `_manager_call()` скрывает различие через `isinstance`.

## Дублирование `hash_password`

См. [02-core §Криптография](02-core.md). Суть: `hash_password()` в `app.py` (600k, `iterations$salt$hash`) и в `database/repositories.py` (100k, `salt$hash`) расходятся; `verify_password()` читает оба.

## Connection reconciliation (деструктивный!)

`GET /api/servers/{id}/connections` при `cached=false` в `routers/connections.py`:
1. SSH → получить live clients с remote
2. **Авто-создаёт** в БД клиентов с remote, отсутствующих локально
3. **Авто-удаляет** из БД клиентов, отсутствующих на remote
4. ⚠️ Потенциально деструктивно при багах в SSH-логике

## Legacy `data.json`

- `DATA_FILE` в `app.py` (env `DATA_FILE`) — путь, который бэкапит `_background_backup`; не источник данных (всё в БД)
- Bearer-аутентификация выполняется `_check_admin()` в `app.py` по таблице `ApiToken`; не добавляй in-memory fallback.
- `scripts/migrate_secrets.py`, `scripts/migrate_to_postgres.py` — one-shot миграции

## Dev vs Production

| Аспект | Dev (`AWP_DEV=1`) | Production |
|---|---|---|
| `SECRET_KEY` | Эфемерный (48-byte) | Обязателен, ≥32 символа, `sys.exit(1)` |
| `MASTER_KEY` | Эфемерный Fernet | Обязателен, валидный Fernet |
| БД | SQLite (`app.db`) | PostgreSQL (asyncpg) |
| `SESSION_COOKIE_SECURE` | `False` | `True` |
| `TrustedHostMiddleware` | Пропущен | Активен |
| PostHog | Опционален | Опционален |
| SSH | Синтезируются | Реальные paramiko |

---

## Словарь терминов

| Термин | Структура | Описание |
|---|---|---|
| **Server** | ORM `Server` | Удалённый VPS, Fernet credentials |
| **Protocol** | ORM `ServerProtocol` | VPN/инфра-сервис (awg, xray...). Multi-instance через `__N` |
| **Connection** | ORM `UserConnection` | Клиентское VPN-подключение (User ↔ Server ↔ Protocol) |
| **Client** | Dict из SSH | Конфиг клиента на remote (ключи, IP, allowed_ips) |
| **User** | ORM `User` | Роли: admin, support, user. Traffic tracking, share |
| **Share** | `User.share_token` | Публичная ссылка на VPN-конфиги |
| **Tunnel** | `TunnelRuntime` | CF/ngrok туннель для доступа к панели |
| **ApiToken** | ORM `ApiToken` | `awp_...` bearer, SHA-256 hash only |
| **AmneziaConfig** | `amnezia_config.py` | Генератор `vpn://` URI и QR |
| **Remnawave** | External API | Синхронизация пользователей (`sync_users_with_remnawave`) |

---

## Priority Reading Order

### Приоритет 1 — Понять контекст
1. `config.py` — env-переменные
2. `database/models.py` — схема данных
3. Секция реестра протоколов в `app.py` — протоколы, фабрика, dispatch

### Приоритет 2 — Добавить эндпоинт
4. `routers/connections.py` — эталонный CRUD + SSH + DB + auth
5. `app.py` — `_check_admin`, `get_current_user`, `tpl()`
6. `middleware.py` — CSRF, headers

### Приоритет 3 — Добавить протокол
7. `managers/awg_manager.py` — эталонный менеджер
8. `managers/ssh_manager.py` — SSH lifecycle
9. `get_protocol_manager()` в `app.py` — регистрация менеджеров
10. `BASE_PROTOCOLS` / `MULTI_INSTANCE_PROTOCOLS` в `app.py`

### Приоритет 4 — Telegram-бот
11. `_run_bot()` и `_dispatch()` в `telegram_bot.py` — polling + dispatch
12. `_ref()` / `_resolve_ref()` в `telegram_bot.py`
13. `_get_ssh_and_manager()` в `telegram_bot.py` — SSH из бота

### Приоритет 5 — БД-схема
14. `database/repositories.py` — ServerRepository (encrypt on write!)
15. `migrations/env.py` — async Alembic
16. `database/session.py` — engine, pool
