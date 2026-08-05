# Amnezia Web Panel — AI Agent Guide

## Окружение

- Весь Python только через `venv/`: `venv/bin/python`, `venv/bin/pip`, `venv/bin/pytest`
- Не использовать `sudo` на хосте
- Проверка синтаксиса: `venv/bin/python -m py_compile <file>`
- Тесты: `venv/bin/python -m pytest`

## Что это за проект

Self-hosted панель управления VPN-серверами. Один Docker-контейнер управляет множеством удалённых серверов по SSH, разворачивая WireGuard, AmneziaWG, Xray, SOCKS5, AdGuard Home, Nginx, DNS и Telegram-бота.

**Стек:** Python 3.12+, FastAPI, SQLAlchemy (async), paramiko, Docker Compose.
**Dev-режим:** `AWP_DEV=1` — SQLite, синтез ключей, отключение TrustedHost.
**Production:** PostgreSQL (asyncpg), Fernet-шифрование секретов, обязательные `SECRET_KEY` и `MASTER_KEY`.

## Структура проекта

```
app.py              — точка входа, FastAPI app, роутинг протоколов, туннели, утилиты (~2150 строк)
telegram_bot.py     — Telegram-бот (~1680 строк)
config.py           — все настройки из env/.env, валидация ключей при старте
middleware.py        — CSRF, SecurityHeaders, SlowAPI
managers/           — protocol managers (duck-typing, без базового класса)
  ssh_manager.py    — paramiko-обёртка, ЕДИНСТВЕННЫЙ модуль, открывающий SSH-сокеты
  secrets.py        — Fernet-шифрование секретов серверов (формат v1:<token>)
  wireguard_manager.py, awg_manager.py, xray_manager.py, socks5_manager.py,
  adguard_manager.py, nginx_manager.py, dns_manager.py, telemt_manager.py,
  backup_manager.py
routers/            — FastAPI роутеры (auth, servers, protocols, connections,
                      users, sharing, settings, templates, api_tokens)
database/
  models.py         — ORM: User, Server, ServerProtocol, UserConnection, ApiToken, Setting
  session.py        — get_db (FastAPI dependency), get_db_context (context manager)
  repositories.py   — высокоуровневые операции (ServerRepository, UserRepository)
  crud.py           — CRUD для таблицы settings (key-value JSON)
templates/          — Jinja2 HTML-шаблоны
static/             — CSS, JS
tests/              — pytest + FastAPI TestClient, in-memory SQLite
docs/ARCHITECTURE.md — глубокий SSOT: middleware, протоколы, SSH, безопасность
```

## Ключевые паттерны

### Роутер (эндпоинт)

Роутеры импортируют `app.py` как `app_module` для доступа к общим функциям (`_check_admin`, `_manager_call` и т.д.). Это обход циклических импортов — не меняй этот паттерн.

```python
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from database.session import get_db
import app as app_module

router = APIRouter(tags=["Example"])

@router.post("/api/example/action")
async def example_action(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await app_module._check_admin(db, request)
    if not user:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    # ... бизнес-логика ...
    return JSONResponse({"status": "ok"})
```

### Вызов protocol manager

`WireGuardManager` — исключение: его методы НЕ принимают `protocol_type` первым аргументом. `_manager_call` скрывает это различие:

```python
ssh = SSHManager(host, port, username, password, private_key)
ssh.connect()
try:
    app_module.ensure_docker_installed(ssh)
    manager = app_module.get_protocol_manager(ssh, protocol)
    result = await app_module._manager_call(
        manager, "install", protocol, *args, **kwargs
    )
finally:
    ssh.disconnect()
```

### Работа с БД

- В роутах: `db: AsyncSession = Depends(get_db)` — сессия живёт в рамках запроса
- Вне роутов (фоновые задачи, бот): `async with get_db_context() as session:`
- Коммит всегда явный: `await db.commit()`
- `expire_on_commit=False`, `autoflush=False` — объекты остаются валидными после коммита

### Реестр протоколов

```python
# app.py — добавляй новые протоколы сюда:
BASE_PROTOCOLS = ['awg', 'awg2', 'awg_legacy', 'xray', 'telemt', 'dns',
                  'wireguard', 'socks5', 'adguard', 'nginx']
MULTI_INSTANCE_PROTOCOLS = {'awg', 'awg2', 'awg_legacy', 'xray', 'telemt', 'socks5'}
```

`MULTI_INSTANCE_PROTOCOLS` — протоколы, которые могут иметь несколько инстансов на одном сервере (каждый со своим `protocol_type` вроде `awg`, `awg_2`, `xray_custom`).

## Безопасность (критично)

### SSH-пароли

Передаются через paramiko stdin-канал. **НИКОГДА** не используй:
```bash
# ❌ Утекает в /proc/<pid>/cmdline
echo 'password' | sudo -S command
```
Правильно — через `SSHManager.run_sudo_command()`, который передаёт пароль через stdin.

### Валидация пользовательского ввода

Все строки от пользователя (домены, email, имена) валидируются ДО интерполяции в shell:
- `nginx_manager._validate_domain()` / `_validate_email()`
- Пути: `shlex.quote()`

### Секреты

- Пароли/ключи серверов шифруются Fernet: `encrypt_secret()` / `decrypt_secret()` из `managers/secrets.py`
- API-токены: формат `awp_<32 chars>`, хранятся как SHA-256 хэш, никогда в открытом виде
- Host key pinning: `_PinningHostKeyPolicy` — отпечаток пиннится после первого успешного handshake

### Middleware (порядок важен)

Внешний → внутренний: `TrustedHost → SecurityHeaders → SlowAPI → CSRF → Session`

- CSRF: double-submit cookie, Bearer-запросы exempt
- `TrustedHost`: только в production (пропускается при `AWP_DEV=1`)

## Чего НЕ делать

- **Не добавляй глобальное состояние** (глобальные dict/list для хранения данных) — всё состояние в БД
- **Не используй синхронный DB-доступ** вне `run_in_executor` — проект полностью async
- **Не создавай файлы в корне проекта** — роутеры в `routers/`, менеджеры в `managers/`
- **Не ломай `import app as app_module`** — это осознанный обход циклических импортов
- **Не добавляй `try/except` без логирования** — ошибки не должны теряться молча
- **Не используй `data.json`** — это legacy, всё хранится в БД через SQLAlchemy
- **Не добавляй `echo '<pass>' | sudo -S`** в shell-скрипты — используй paramiko stdin

## Тесты

**Запуск:** `venv/bin/python -m pytest` (или `venv/bin/python -m pytest tests/test_auth.py -v`)

**Инфраструктура:**
- `conftest.py` — in-memory SQLite, автосоздание схем, дефолтный admin-пользователь
- `app_client` fixture — `FastAPI TestClient` с `raise_server_exceptions=False`
- CSRF: перед POST-запросом сделай GET для получения `csrf_token` cookie

**Паттерн теста:**
```python
class TestSomething:
    def test_action_success(self, app_client):
        c = app_client
        c.get("/login")  # получить csrf cookie
        csrf = c.cookies.get("csrf_token", "")

        r = c.post(
            "/api/something/action",
            json={"key": "value"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
```

**Для тестов без CSRF** (API-токены): используй `Authorization: Bearer awp_...` — bearer-запросы обходят CSRF.

**Менеджеры:** тесты в `tests/managers/`. SSH-соединения мокаются, не открывай реальные сокеты в тестах.

## Legacy и миграции

- `data.json` — **не используется**, данные перенесены в БД. Функции `load_data`/`save_data` удалены
- `_maybe_migrate_legacy_data_file()` — one-time перенос при первом запуске после миграции
- `_resolve_api_token()` — работает со словарём в памяти (legacy), новые токены пишутся в таблицу `ApiToken`

## Навигация по большим файлам

`app.py` (~2150 строк) и `telegram_bot.py` (~1680 строк) — используй grep по сигнатурам:
```bash
grep -n "def \|async def \|class " app.py
grep -n "BASE_PROTOCOLS\|_check_admin\|_manager_call\|get_protocol_manager" app.py
```

## Документация

- Этот файл (`AGENTS.md`) — быстрый старт
- `docs/ARCHITECTURE.md` — глубокий SSOT: middleware, протоколы, SSH-слой, безопасность, паттерн расширения протокола (7-шаговый чек-лист в §13)
- Если изменение меняет поведение, сигнатуры или структуру — обнови соответствующий документ
