# Amnezia Web Panel — Карта местности

> Self-hosted VPN-панель. Один контейнер управляет серверами по SSH, разворачивая WireGuard/AWG/Xray/SOCKS5/AdGuard/Nginx/DNS/Telegram.

**Паттерн:** Monolithic Service Layer — FastAPI + массивное ядро в `app.py` (~2250 строк) + protocol managers.
**Стек:** Python 3.12+, FastAPI, SQLAlchemy (async), paramiko, Docker.

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
managers/           — 10 protocol managers + ssh_manager + secrets + amnezia_config
database/           — models.py, session.py, crud.py, repositories.py
templates/          — 8 Jinja2 HTML-шаблонов
translations/       — i18n (en, ru, fa, fr, zh)
tests/              — pytest + FastAPI TestClient
scripts/            — one-shot миграции (secrets, postgres)
```

## Ключевые константы

```python
BASE_PROTOCOLS = ['awg', 'awg2', 'awg_legacy', 'xray', 'telemt', 'dns',
                  'wireguard', 'socks5', 'adguard', 'nginx']        # app.py:954
MULTI_INSTANCE_PROTOCOLS = {'awg','awg2','awg_legacy','xray','telemt','socks5'}  # :955
API_TOKEN_PREFIX = 'awp_'                                           # :1121
CURRENT_VERSION = 'v1.5.0'                                          # :192
```

## Циклический импорт (осознанный паттерн)

Роутеры импортируют `app.py` как `import app as app_module` для доступа к `_check_admin`, `get_ssh`, `get_protocol_manager`. `app.py` импортирует роутеры в **конце файла** (строка 2020). `app.py:127` — `sys.modules['app'] = sys.modules[__name__]` для корректной работы при `__main__`.

## 3 механизма аутентификации

| Механизм | Триггер | Проверка |
|---|---|---|
| Session cookie | HTML + большинство API | `request.session['user_id']` → `get_current_user()` (`:1462`) |
| Bearer token | `Authorization: Bearer awp_...` | SHA-256 сверка (`_resolve_api_token`, `:1137`) |
| Share token | `/share/{token}` | `UserRepository.get_by_share_token()` |

## ORM-схема (кратко)

```
User (uuid) ──1:N── UserConnection (uuid) ──N:1── Server (int)
User ──1:N── ApiToken (uuid)                 Server ──1:N── ServerProtocol (int)
Setting (key:PK, value:JSON)
```

Каскады: `cascade="all, delete-orphan"` + FK `ondelete="CASCADE"`.
Fernet-шифрование: `Server.password`, `Server.private_key` (lazy через `@property`).
