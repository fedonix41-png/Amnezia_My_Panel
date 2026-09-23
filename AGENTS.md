# Amnezia Web Panel — AI Agent Guide

## Окружение

- Весь Python запускать только через `venv/`: `venv/bin/python`, `venv/bin/pip`, `venv/bin/pytest`.
- Не использовать `sudo` на хосте.
- Проверка синтаксиса: `venv/bin/python -m py_compile <file>`.
- Тесты: `venv/bin/python -m pytest` или точечно `venv/bin/python -m pytest <test> -q`.
- Тесты используют in-memory SQLite и моки SSH; они не имеют доступа к production-серверам.

## Что это за проект

Self-hosted FastAPI-панель управляет удалёнными VPN-серверами по SSH. Она разворачивает WireGuard, AmneziaWG, Xray, SOCKS5, AdGuard Home, Nginx, DNS и Telemt, а также включает Telegram-бота.

**Стек:** Python 3.12+, FastAPI, async SQLAlchemy, paramiko, Docker Compose.

- Dev: `AWP_DEV=1`, SQLite, эфемерные ключи, TrustedHost отключён.
- Production: PostgreSQL/asyncpg, Fernet-шифрование, обязательные `SECRET_KEY` и `MASTER_KEY`.

## Как собирать контекст

Не читай всю кодовую базу перед каждой правкой. До редактирования проследи затрагиваемый поток:

1. endpoint, callback или background entry point;
2. вызываемые helpers и manager methods;
3. ORM-модели, repositories и точки `commit`;
4. потребители API в templates/JavaScript и Telegram-боте;
5. существующие тесты и непокрытые ветки.

Для сложной межслойной правки сначала кратко зафиксируй инварианты и минимальный набор файлов. Не выводи контракт только из одного manager или router.

## Карта кода

- `app.py` — FastAPI app, auth helpers, protocol registry/factory, tunnels, background jobs, lifespan.
- `routers/` — HTTP API и HTML entry points.
- `managers/` — SSH и реализации протоколов.
- `database/` — ORM, sessions, repositories, settings CRUD.
- `telegram_bot.py` — in-process long-polling без `python-telegram-bot`.
- `templates/` — Jinja2 и значительная часть frontend JavaScript.
- `static/` — CSS и статические assets.
- `tests/` — pytest, FastAPI TestClient, in-memory SQLite, SSH mocks.
- `docs/terrain/INDEX.md` — тематический указатель; читай только разделы, связанные с задачей.

## Ключевые контракты

### Routers

- Новые endpoints размещать в `routers/`, а не в `app.py`.
- Роутеры импортируют `app as app_module` для общих helpers. Это осознанный обход циклических импортов.
- DB session в endpoint: `db: AsyncSession = Depends(get_db)`.
- Реэкспорт из `app.py` нужен только если есть существующий импорт, test patch target или вызов через `app`.

### Protocol managers

Единого Python-интерфейса нет. Сигнатуры исторически различаются, поэтому перед вызовом или изменением проверь реальную сигнатуру и существующие call sites.

- `get_protocol_manager(ssh, protocol)` учитывает multi-instance key.
- `_manager_call(manager, method, protocol, ...)` пропускает `protocol` только для `WireGuardManager`; он не унифицирует все остальные различия.
- Install/uninstall вызывают `install_protocol`/`remove_container` по веткам в `routers/protocols.py`; не заменяй эти ветки общим вызовом без проверки всех managers.
- `WireGuardManager` обычно не принимает `protocol_type`.
- Xray получает protocol key в конструкторе, но часть client methods также принимает protocol.
- Новый протокол потребует проверки registry, factory, names/container names, API, traffic sync, Telegram, config/QR и tests.

### Database

- В роутах — request-scoped `AsyncSession`; вне роутов — `async with get_db_context()`.
- Коммит явный: `await db.commit()`.
- `expire_on_commit=False`, `autoflush=False`.
- Соблюдай текущий стиль слоя: в проекте есть и repositories, и прямые async SQLAlchemy queries в routers.
- `data.json` — legacy backup target, а не источник состояния.

### SSH и секреты

- Только `managers/ssh_manager.py` открывает SSH-сокеты; managers получают `SSHManager` через конструктор.
- Sudo password передаётся через stdin в `run_sudo_command()`/`run_sudo_script()`, никогда через command line.
- Пользовательские домены, email, имена и пути валидируются до shell interpolation; пути квотируются `shlex.quote()`.
- Server credentials — Fernet `v1:<token>` через `managers/secrets.py`.
- API tokens — `awp_...`, в БД только SHA-256 digest.
- Host keys защищены TOFU pinning в `_PinningHostKeyPolicy`.

## Безопасные границы

- Не добавляй новое process-global state. Существующие исключения: `TUNNEL_RUNTIMES`, `_callback_refs`, `_pending_inputs`.
- Не ломай `import app as app_module` без отдельного плана декомпозиции.
- Не создавай feature-файлы в корне: endpoints идут в `routers/`, protocol logic в `managers/`, общие services — в текущий подходящий package.
- Не добавляй `try/except` без логирования, если ошибка не является ожидаемой и безопасно игнорируемой.
- Не открывай реальные SSH-сокеты в тестах.

## Тесты

- HTTP tests: `app_client`; CSRF cookie получается через `GET /login`.
- Bearer requests освобождены от CSRF.
- Мокай `SSHManager`, `get_ssh` и protocol managers на границе router↔remote host.
- Для изменения endpoint проверь success path, permission/error path и DB side effects.
- Сначала запускай целевые тесты; полный suite — после их успеха.

## Документация по задаче

- API/auth/middleware: `docs/terrain/01-entry-points.md`.
- Protocol registry, manager factory, crypto, config formats: `02-core.md` и `06-vpn-formats.md`.
- DB, SSH, tunnels, background jobs: `03-infrastructure.md`.
- Cross-layer flows: `04-critical-paths.md`.
- Опасные legacy-контракты: `05-gotchas.md`.
- Deployment и расширение протоколов: `07-deployment.md`.

Если правка меняет поведение, сигнатуру или структуру, обнови только связанные terrain-разделы. В документации ссылайся на имена символов, а не на номера строк.
