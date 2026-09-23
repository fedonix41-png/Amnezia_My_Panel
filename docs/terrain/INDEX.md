# Amnezia Web Panel — Карта местности

> Self-hosted VPN-панель. Один контейнер управляет серверами по SSH, разворачивая WireGuard/AWG/Xray/SOCKS5/AdGuard/Nginx/DNS/Telegram.

**Паттерн:** Monolithic Service Layer — FastAPI + крупное ядро в `app.py` + protocol managers.
**Стек разработки:** Python, FastAPI, SQLAlchemy (async), paramiko. Локальный запуск: `venv/bin/uvicorn app:app --reload --port 5000`; `docker-compose.dev.yml` нужен только для PostgreSQL.

Это тематический указатель, а не обязательный pre-read. Перед правкой читай только связанные с задачей разделы и проверяй их по живому коду.

## Файлы документации

| Файл | Содержание | Когда читать |
|---|---|---|
| [01-entry-points.md](01-entry-points.md) | Эндпоинты, HTML-страницы, Telegram-бот, аутентификация, middleware | Добавляешь эндпоинт или меняешь auth |
| [02-core.md](02-core.md) | Реестр протоколов, фабрика менеджеров, криптография, i18n, config.py | Добавляешь протокол, меняешь crypto |
| [03-infrastructure.md](03-infrastructure.md) | БД, SSH, туннели, WARP, фоновые задачи, PostHog, rate limiting | Работаешь с БД, SSH, деплоем |
| [04-critical-paths.md](04-critical-paths.md) | 4 пошаговых data flow + граф зависимостей | Разбираешься в сложном процессе |
| [05-gotchas.md](05-gotchas.md) | Подводные камни, глоссарий, dev/prod, priority reading, managers | Первое знакомство, отладка |
| [06-vpn-formats.md](06-vpn-formats.md) | VPN config форматы: vpn://, QR binary, AWG obfuscation, native JSON | Работаешь с конфигами/QR |
| [07-deployment.md](07-deployment.md) | Docker hardening, SSL, WARP, protocol extension, smoke test | Деплой, новый протокол |

## Быстрый старт — что где лежит

```
app.py              — ядро: хелперы, протоколы, tunnels, auth, startup/shutdown
config.py           — env-переменные, dev/prod валидация
middleware.py       — CSRF, SecurityHeaders, PostHog middleware
telegram_bot.py     — Telegram-бот (raw httpx, long-polling)
routers/            — 9 FastAPI-роутеров (auth, servers, protocols, connections,
                      users, sharing, settings, templates, api_tokens)
managers/           — protocol/infrastructure managers + secrets + amnezia_config
database/           — models.py, session.py, crud.py, repositories.py
templates/          — 9 Jinja2 HTML-шаблонов с frontend JavaScript
translations/       — i18n (en, ru, fa, fr, zh)
tests/              — pytest + FastAPI TestClient
scripts/            — one-shot миграции (secrets, postgres)
```

## Ключевые константы

```python
BASE_PROTOCOLS = ['awg', 'awg2', 'awg_legacy', 'xray', 'telemt', 'dns',
                  'wireguard', 'socks5', 'adguard', 'nginx']
MULTI_INSTANCE_PROTOCOLS = {'awg','awg2','awg_legacy','xray','telemt','socks5'}
API_TOKEN_PREFIX = 'awp_'
CURRENT_VERSION = 'v1.5.0'
```

## Циклический импорт (осознанный паттерн)

Роутеры импортируют `app.py` как `import app as app_module` для доступа к `_check_admin`, `get_ssh`, `get_protocol_manager`. `app.py` импортирует роутеры в секции `ROUTERS REGISTRATION`. Алиас `sys.modules['app'] = sys.modules[__name__]` поддерживает корректный импорт при запуске `app.py` как `__main__`.

## 3 механизма аутентификации

| Механизм | Триггер | Проверка |
|---|---|---|
| Session cookie | HTML + большинство API | `request.session['user_id']` → `get_current_user()` |
| Bearer token | `Authorization: Bearer awp_...` | `_check_admin()` вычисляет SHA-256, ищет `ApiToken`, проверяет owner role/enabled и обновляет `last_used_at` с throttling |
| Share token | `/share/{token}` | `UserRepository.get_by_share_token()` |

## ORM-схема (кратко)

```
User (uuid) ──1:N── UserConnection (uuid) ──N:1── Server (int)
User ──1:N── ApiToken (uuid)                 Server ──1:N── ServerProtocol (int)
Setting (key:PK, value:JSON)
```

Каскады: `cascade="all, delete-orphan"` + FK `ondelete="CASCADE"`.
Fernet-шифрование: `Server.password`, `Server.private_key` (lazy через `@property`).

Bearer-аутентификация не имеет отдельного resolver-а: административные роуты используют `_check_admin()` из `app.py`.
