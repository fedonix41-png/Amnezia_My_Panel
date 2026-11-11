---
name: "telegram-bot-architecture"
description: "Активировать при модификации логики Telegram-бота (telegram_bot.py), добавлении команд, callback-кнопок или меню."
---
# Архитектура Telegram-бота

1. **Транспорт**: Самописный in-process long-polling поверх `httpx.AsyncClient` в `telegram_bot.py` (запускается через `launch_bot`). Библиотека `python-telegram-bot` для логики не используется.
2. **Состояние в памяти**: Допустимые process-global структуры бота — `_callback_refs` и `_pending_inputs`. Новое глобальное состояние не добавлять.
3. **Безопасность**: Административные действия обязательно проверять через `_require_admin(...)`.
4. **Переиспользование логики**: Использовать общие сервисы из `services/` и менеджеры протоколов через `_get_ssh_and_manager` / `_manager_call`, не дублируя низкоуровневые SSH-команды.
5. **Глубокий контекст (читать по требованию)**:
   - `docs/terrain/01-entry-points.md` — архитектура и команды Telegram-бота.
   - `docs/terrain/05-gotchas.md` — особенности состояния и callback-ссылок.
