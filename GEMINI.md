# Antigravity Workspace — GEMINI.md

Проектные конвенции — в `AGENTS.md`. Этот файл: директивы навигации по кодовой базе, реестр субагентов, workflow'ы Antigravity.

## Навигация по коду

Файлы ядра большие — не читай целиком без необходимости. Паттерн:

- `app.py` (~2233 строк), `routers/*.py`, `telegram_bot.py` (~51KB) → **grep по сигнатуре/декоратору → read с offset/limit → ссылайся как `file:line`.**
- Глубокий контекст (схема данных, реестр протоколов, security-границы) → `docs/ARCHITECTURE.md`.
- Разведка незнакомой области → Task-субагент `explore` или `research` (Read-Only), не загружай файлы в основной контекст.

## Якоря частых точек входа

- `_check_admin`: `app.py:1450`
- Middleware: `app.py:125-145`
- `load_data`: `app.py:216`
- `get_ssh`: `app.py:256`
- `DATA_FILE`: `app.py:156`
- Монтирование роутеров: `app.py:2020-2035`
- `BASE_PROTOCOLS`: `app.py:902`
- `get_protocol_manager`: `app.py:999`
- SSH host key pinning: `ssh_manager.py:104-114`
- `build_initial_server_json`: `xray_manager.py:13`

## Субагенты

- **`explore` / `research`** (Read-Only) — разведка кода, документации, внешних библиотек без засорения контекста.
- **`protocol-builder`** (Write) — новый VPN/Proxy протокол: `managers/`, `routers/protocols.py`, `app.py`. Чек-лист: `docs/ARCHITECTURE.md` §13.
- **`security-auditor`** (Read-Only) — аудит SSH (paramiko stdin), Fernet (`v1:`), `shlex.quote`, эндпоинты.
- **`refactor-auditor`** (Write) — рефакторинг с сохранением 100% обратной совместимости и зелёным pytest.

## Workflow'ы

- Крупные автономные задачи → `/goal` с итерациями до полного выполнения.
- Перед крупными архитектурными правками → `implementation_plan.md` в Planning Mode.
- **Обязательная верификация**: ни одно изменение не считается завершённым без `venv/bin/python -m pytest`.

## Инструментальные заметки

- Перед CLI-командой может появиться блок `[kcp] ...` (opencode-kcp-plugin) со структурой флагов — используй его, не запускай `--help`.
- При длинном хвосте сессии перед серией правок → `/compact`.
