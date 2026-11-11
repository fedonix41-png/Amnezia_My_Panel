---
name: "fastapi-router-architecture"
description: "Активировать при создании новых эндпоинтов, добавлении роутеров, сервисов в services/ или модификации HTTP API."
---
# Архитектура FastAPI и роутеров

1. **Модульность**: Новые эндпоинты добавляются только в `routers/`, а общая для роутеров и Telegram-бота бизнес-логика — в `services/` (например, `services/server_stats.py`), не раздувая `app.py`.
2. **Инъекция зависимостей**: Использовать `db: AsyncSession = Depends(get_db)` для получения сессии БД в роутерах.
3. **Доступ к ядру**: Для вызова общих хелперов (`_check_admin`, `get_ssh`, `get_protocol_manager`, `_manager_call`) импортировать `import app as app_module` внутри роутеров.
4. **Обратная совместимость**: Ре-экспорт функций в `app.py` нужен только если на символ уже ссылаются тесты, патч-таргеты или `telegram_bot.py`.
5. **Глубокий контекст (читать по требованию)**:
   - `docs/terrain/01-entry-points.md` — карта эндпоинтов, 3 механизма auth (Session, Bearer `awp_`, Share token) и стек middleware.
   - `docs/terrain/04-critical-paths.md` — сквозные потоки запросов.
