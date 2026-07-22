# Amnezia Web Panel — AGENTS.md

Это **lean-роутер**: автозагружается в каждую сессию, держится компактным (не подвергается compaction). Глубокий архитектурный контекст — по требованию в `docs/ARCHITECTURE.md`.

## Стек

- **Python 3.14**, **FastAPI** (ASGI) поверх Starlette, сервер — uvicorn.
- **Окружение**: обязательно проектный `venv` (`venv/bin/python`, `venv/bin/pytest`). Системный Python падает с ModuleNotFoundError — правок на уровне ОС не делать.
- Ядро `app.py` (~2233 строк) + пакет роутеров `routers/*.py` + паттерн `managers/*.py` (по менеджеру на протокол).
- Секреты — симметричное Fernet-шифрование (`managers/secrets.py`, `MASTER_KEY` из env, формат `v1:`).
- Деплой: Docker Compose (non-root, read-only rootfs, cap_drop ALL) + опциональный публичный туннель (cloudflared/ngrok, in-process). Dev-режим — `AWP_DEV=1`.

## Контекстные правила (КРИТИЧНО — экономия токенов)

- `app.py` (~2233 строк) и `routers/*.py` — **НИКОГДА не читать целиком.** Паттерн: Grep по сигнатуре/декоратору → Read конкретного диапазона (offset/limit) → ссылка `file:line`.
- `telegram_bot.py` ~51KB — аналогично.
- **Глубокий контекст** (схема данных, потоки, реестр протоколов, security-границы, паттерн расширения): Grep/Read `docs/ARCHITECTURE.md`. Не подгружать целиком без нужды.
- Разведка незнакомой области — делегируй Task-субагенту (`explore` или `research`), не грузи файлы в основной контекст.
- Перед длинной серией правок при большим хвосте сессии — `/compact`.
- Перед CLI-командой может появиться блок `[kcp] ...` (плагин opencode-kcp-plugin) со структурой флагов. Используй его; не запускай `--help` при наличии `[kcp]`.

## Реестр субагентов (Antigravity Subagents)

- **`research` / `explore`** (Read-Only): разведка фрагментов кода, документации, внешних библиотек без засорения контекста родительской сессии.
- **`protocol-builder`** (Write Enabled): разработка и интеграция новых VPN/Proxy протоколов в `managers/`, `routers/protocols.py` и `app.py` по чек-листу из `docs/ARCHITECTURE.md` §13.
- **`security-auditor`** (Read-Only): аудит безопасности SSH-команд (paramiko stdin), Fernet-шифрования (`v1:`), экранирования (`shlex.quote`) и эндпоинтов.
- **`refactor-auditor`** (Write Enabled): проведение безопасного рефакторинга компонентов и роутеров с выносом логики в модули при гарантированном сохранении 100% обратной совместимости и прохождении pytest.

## Рабочие процессы Antigravity (Workflows & Commands)

- **Автономные крупные задачи**: использовать `/goal` для сложных автономных итераций до полного выполнения цели.
- **Аудит и планирование**: перед крупными архитектурными правками составляется `implementation_plan.md` в Planning Mode.
- **Обязательная верификация**: ни одно изменение не считается завершенным без вызова `venv/bin/python -m pytest`.


## Команды (строго в venv)

- Тесты: `venv/bin/python -m pytest` (pytest/pytest-asyncio/httpx в `requirements-dev.txt`).
- Проверка синтаксиса: `venv/bin/python -m py_compile <file>` (lint/typecheck-тулинга не настроено).
- Запуск локально: `AWP_DEV=1` + `docker-compose.yml` + `docker-entrypoint.sh`.

## Карта файлов (якоря — file:line, подробнее в ARCHITECTURE.md §«Якоря»)

- `app.py` — ядро, `_check_admin` `app.py:1450`, middleware `app.py:125-145`, `load_data` `app.py:216`, `get_ssh` `app.py:256`, alias `sys.modules['app']` `app.py:110`, монтирование роутеров `app.py:2020-2035`. `DATA_FILE` (`app.py:156`) читается из env.

- `routers/` — модульные FastAPI APIRouter: `auth.py`, `templates.py`, `servers.py`, `protocols.py`, `connections.py`, `users.py`, `sharing.py`, `settings.py`, `api_tokens.py`.
- `config.py` — env-конфигурация (`SECRET_KEY`, `MASTER_KEY`, `AWP_DEV`, rate-limits).
- `middleware.py` — CSRF (double-submit), SecurityHeaders.
- `managers/ssh_manager.py` — paramiko, **пиннинг отпечатка после успешного handshake** (`ssh_manager.py:104-114`), sudo через stdin (не cmdline).
- `managers/secrets.py` — Fernet, формат `v1:`.
- `managers/{awg,wireguard,xray,telemt,dns,adguard,nginx,socks5,backup}_manager.py` — протоколы (duck-typed интерфейс, без базового класса).
- `managers/xray_manager.py` — `build_initial_server_json` (`xray_manager.py:13`) — чистая функция-билдер `server.json` (VLESS-Reality + Stats + API).
- `scripts/migrate_secrets.py` — миграция plaintext-секретов в `v1:` (отказ при `AWP_DEV=1` без `--force`).
- `tests/{test_auth,test_api_tokens,test_helpers,test_secrets,test_share}.py`, `tests/managers/test_{awg,wireguard,xray_config}_*.py`, `tests/conftest.py`.

## Безопасность

- Все секреты в `data.json` — только через Fernet (`managers/secrets.py`); SSH-креды хранятся как `v1:`.
- `SECRET_KEY` и `MASTER_KEY` обязательны из env; без них (кроме `AWP_DEV=1`) — refuse to start.
- SSH-пароли — через stdin-канал paramiko, **не** через `echo '<p>' | sudo -S` (не должны светиться в `/proc/<pid>/cmdline`).
- Пользовательские строки (domain/email/username) перед интерполяцией в sudo-команды — валидировать (пример: `nginx_manager._validate_domain`/`_validate_email`); пути квотировать `shlex.quote`.
- `data.json` содержит секреты — не коммитить (в `.gitignore` вместе с `.env`, `*.local`, `venv/`).

## Паттерн расширения (кратко)

Добавление нового протокола: регистрация в `app.py` (`BASE_PROTOCOLS` `app.py:902`, `get_protocol_manager` `app.py:999`, мапы имён/контейнеров) → новый `managers/foo_manager.py` (duck-typed, клиентские методы с `protocol_type` первым аргументом) → ветка установки в `routers/protocols.py`. Подробный чек-лист на 7 шагов — `docs/ARCHITECTURE.md` §13.