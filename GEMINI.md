# Antigravity Workspace

Этот файл дополняет универсальные правила из `AGENTS.md` и карту проекта `docs/terrain/INDEX.md` настройками для Antigravity IDE.

## Экономия контекста и навигация
- **Скиллы по требованию (`.agents/skills/`):** Используй профильные скиллы только когда задача затрагивает соответствующий слой (`vpn-protocol-manager`, `paramiko-ssh-manager`, `fastapi-router-architecture`, `sqlalchemy-db-workflow`, `fernet-secrets-security`, `telegram-bot-architecture`, `long-context-indexer`).
- **Разведка без засорения чата:** Если нужно проанализировать крупные файлы (`managers/awg_manager.py`, `app.py`, `telegram_bot.py`, `templates/server.html`) или найти все точки вызова по проекту, делегируй поиск встроенному read-only субагенту `research`. В основном окне читай только целевые диапазоны строк.
- **Не плоди лишних субагентов:** Для большинства задач достаточно основного агента, `research` (поиск по коду) и `browser` (проверка веб-интерфейса). Создавай кастомных субагентов (`define_subagent`) только для реально параллельных объёмных задач.
- **MCP-инструменты:** Используй `context7` для сверки с актуальной документацией FastAPI / SQLAlchemy / Paramiko / Alembic и `postgres` при проверке локальной БД `amnezia_db_dev`.

## Рабочий процесс
- **Безопасность ОС и тесты:** Соблюдай `AGENTS.md` §Окружение (`venv/bin/python`, `venv/bin/pytest`, без `sudo`).
- **Планирование:** Перед крупными межслойными изменениями согласуй короткий план.
- **Отчёт после выполнения:** Кратко укажи изменённые файлы/функции, обновлённые разделы `docs/terrain/` и команду проверки.
