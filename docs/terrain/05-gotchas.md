# Подводные камни, Глоссарий, Dev/Prod

> Связан: [INDEX](INDEX.md) | [02-core](02-core.md) | [03-infra](03-infrastructure.md)

## Глобальные мутабельные состояния

⚠️ Всё состояние хранится в БД. Исключения — процессные модульные глобалы, опасные при рефакторинге:

- `TUNNEL_RUNTIMES` (`app.py:226`) — `dict[str, TunnelRuntime]` с `Popen`/PID, мутируется из watcher-потоков. Все мутации под `TUNNEL_LOCK` (`app.py:230`).
- `_callback_refs` (`telegram_bot.py:90`) и `_pending_inputs` (`telegram_bot.py:91`) — обычные dict без `asyncio.Lock`. Безопасны при одном polling-потоке, но race возможен при добавлении конкурентных обработчиков.

Остальные глобалы (`posthog_client`, `TRANSLATIONS`, `DATA_FILE`) — read-only после startup, для агента не критичны.

## Магические числа

Только те, что легко случайно нарушить при рефакторинге:

| Значение | Где | Почему важно |
|---|---|---|
| `600_000` / `100_000` | `app.py:1180` / `repositories.py:20` | PBKDF2 итерации — расходятся, менять согласованно с `verify_password` |
| `300` (5 мин) | `app.py:1122` `API_TOKEN_TOUCH_INTERVAL` | Троттлинг перезаписи `last_used` |
| `25` (сек) | `telegram_bot.py:1565` | Long-polling timeout |
| `15` (сек) | `ssh_manager.py:78` | SSH connect timeout |

## WireGuardManager — исключение в сигнатуре

См. [02-core §Диспетчеризация](02-core.md) — единственный источник истины. `_manager_call()` скрывает различие через `isinstance`.

## Дублирование `hash_password`

См. [02-core §Криптография](02-core.md). Суть: `app.py:1178` (600k, `iterations$salt$hash`) и `repositories.py:20` (100k, `salt$hash`) расходятся; `verify_password()` читает оба.

## Connection reconciliation (деструктивный!)

`GET /api/servers/{id}/connections` при `cached=false` (`connections.py:54`):
1. SSH → получить live clients с remote
2. **Авто-создаёт** в БД клиентов с remote, отсутствующих локально
3. **Авто-удаляет** из БД клиентов, отсутствующих на remote
4. ⚠️ Потенциально деструктивно при багах в SSH-логике

## Legacy `data.json`

- `DATA_FILE` (`app.py:187`, env `DATA_FILE`) — путь, который бэкапит `_background_backup`; не источник данных (всё в БД)
- `_resolve_api_token()` (`app.py:1137`) — legacy dict fallback
- `scripts/migrate_secrets.py`, `scripts/migrate_to_postgres.py` — one-shot миграции

## Dev vs Production

| Аспект | Dev (`AWP_DEV=1`) | Production |
|---|---|---|
| `SECRET_KEY` | Эфемерный (48-byte) | Обязателен, ≥32 символа, `sys.exit(1)` |
| `MASTER_KEY` | Эфемерный Fernet | Обязателен, валидный Fernet |
| БД | SQLite (`app.db`) | PostgreSQL (asyncpg) |
| `SESSION_COOKIE_SECURE` | `False` | `True` |
| `TrustedHostMiddleware` | Пропущен | Активен |
| PostHog | **Обязателен** (RuntimeError!) | Опционален |
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
1. `config.py` (~170 строк) — env-переменные
2. `database/models.py` (~115 строк) — схема данных
3. `app.py:950–1110` — протоколы, фабрика, dispatch

### Приоритет 2 — Добавить эндпоинт
4. `routers/connections.py` — эталонный CRUD + SSH + DB + auth
5. `app.py:1462–1530` — `_check_admin`, `get_current_user`, `tpl()`
6. `middleware.py` (~137 строк) — CSRF, headers

### Приоритет 3 — Добавить протокол
7. `managers/awg_manager.py` — эталонный менеджер (1378 строк)
8. `managers/ssh_manager.py` (285 строк) — SSH lifecycle
9. `app.py:1030–1056` — регистрация в `get_protocol_manager()`
10. `app.py:954–955` — `BASE_PROTOCOLS` / `MULTI_INSTANCE_PROTOCOLS`

### Приоритет 4 — Telegram-бот
11. `telegram_bot.py:1550–1660` — polling + dispatch
12. `telegram_bot.py:250–270` — `_ref()` / `_resolve_ref()`
13. `telegram_bot.py:414` — SSH из бота

### Приоритет 5 — БД-схема
14. `database/repositories.py` — ServerRepository (encrypt on write!)
15. `migrations/env.py` — async Alembic
16. `database/session.py` — engine, pool
