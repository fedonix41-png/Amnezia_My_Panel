# Antigravity Workspace

Этот файл дополняет универсальные правила из `AGENTS.md` настройками для Antigravity IDE.

## Экономия контекста и навигация
- **Граф кода через Repowise (первый шаг):** Перед чтением монолитов (`managers/awg_manager.py`, `app.py`, `telegram_bot.py`, `managers/awg_plugin_updater.py`, `managers/wireguard_manager.py`, `templates/server.html`) или поиском точек вызова сначала обращайся к MCP-серверу `repowise_repowise` (`get_context`, `get_symbol`, `get_risk`, `search_codebase`), а затем читай только узкий диапазон строк.
- **Скиллы по требованию (`.agents/skills/`):** Используй профильные скиллы только когда задача затрагивает соответствующий слой (`vpn-protocol-manager`, `paramiko-ssh-manager`, `fastapi-router-architecture`, `sqlalchemy-db-workflow`, `fernet-secrets-security`, `telegram-bot-architecture`, `long-context-indexer`).
- **Разведка без засорения чата:** Если задача требует широкого обхода многих файлов сверх графа Repowise, делегируй поиск встроенному read-only субагенту `research`. Не плоди лишних кастомных субагентов (`define_subagent`) без реальной параллельной нагрузки (достаточно основного агента, `research` и `browser`).
- **Другие MCP-инструменты:** Используй `context7` для сверки с актуальной документацией FastAPI / SQLAlchemy / Paramiko / Alembic и `postgres` при проверке локальной БД `amnezia_db_dev`.

## Рабочий процесс
- **Безопасность ОС и тесты:** Соблюдай `AGENTS.md` §Окружение и §Тесты (запуск через `venv/bin/...` и `repowise distill venv/bin/python -m pytest -q`, без `sudo`).
- **Планирование:** Перед крупными межслойными изменениями согласуй короткий план.
- **Отчёт после выполнения:** Кратко укажи изменённые файлы/функции, обновлённые разделы `docs/terrain/` и команду проверки.
