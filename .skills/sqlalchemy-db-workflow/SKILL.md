---
name: "sqlalchemy-db-workflow"
description: "Активировать при работе с моделями данных, миграциями или CRUD-операциями."
---
# Работа с базой данных

1. **ORM**: Использовать только SQLAlchemy в асинхронном режиме (asyncpg для PostgreSQL, aiosqlite для SQLite).
2. **Legacy data.json**: Файл `data.json` устарел. Все новые данные должны храниться в БД. Не использовать функции `load_data`/`save_data`/`DATA_LOCK`.
3. **Репозитории**: Для высокоуровневых операций использовать классы из `database/repositories.py` (например, `UserRepository`, `ServerRepository`).
4. **Настройки**: Для key-value настроек использовать таблицу `settings` через `get_setting(db, key, default)` и `set_setting(db, key, value)` из `database/crud.py`.