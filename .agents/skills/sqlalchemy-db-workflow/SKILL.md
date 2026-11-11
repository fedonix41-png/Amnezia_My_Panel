---
name: "sqlalchemy-db-workflow"
description: "Активировать при работе с моделями данных, миграциями Alembic, репозиториями или CRUD-операциями."
---
# Работа с базой данных (Async SQLAlchemy)

1. **Сессии и транзакции**: В роутах использовать `db: AsyncSession = Depends(get_db)`, вне роутов (фоновые задачи, Telegram-бот) — `async with get_db_context() as db:`. Настройки сессии: `expire_on_commit=False`, `autoflush=False`. Коммит всегда явный: `await db.commit()`.
2. **Репозитории и CRUD**: Использовать репозитории из `database/repositories.py` (`UserRepository`, `ServerRepository`) и key-value хелперы `get_setting` / `set_setting` из `database/crud.py`.
3. **Миграции**: Генерировать через `venv/bin/alembic revision --autogenerate -m "<msg>"`, применять через `venv/bin/alembic upgrade head`.
4. **Legacy `data.json`**: Используется только как цель резервного копирования (`_background_backup`), а не источник состояния.
5. **Глубокий контекст (читать по требованию)**:
   - `docs/terrain/03-infrastructure.md` — схема таблиц, каскады и работа с БД.
   - `docs/terrain/05-gotchas.md` — инварианты `UserConnection` и фильтрация фантомных подключений при удалении протоколов.
