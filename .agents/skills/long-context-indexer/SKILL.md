---
name: "long-context-indexer"
description: "Активировать при масштабном рефакторинге, поиске по крупным файлам проекта или анализе межслойных связей."
---
# Экономия контекста и навигация по коду

1. **Не читать монолиты целиком (сначала Repowise)**: Для `managers/awg_manager.py`, `app.py`, `telegram_bot.py`, `managers/awg_plugin_updater.py`, `managers/wireguard_manager.py` и `templates/server.html` сначала запрашивать граф вызовов и сигнатуры через MCP-инструменты Repowise (`get_context`, `get_symbol`, `get_risk`), после чего читать **только узкий диапазон строк** (`StartLine`/`EndLine`).
2. **Тематическая память (`docs/terrain/`)**: Вместо обхода всей кодовой базы сначала сверяйся с `docs/terrain/INDEX.md` и открывай ровно один профильный файл (`01`..`07`).
3. **Делегирование поиска**: Если нужно проследить вызовы по 5+ файлам вне графа Repowise, делегируй разведку read-only субагенту `research`, чтобы в основной диалог вернулась только краткая сводка.
4. **Исключения при поиске**: Игнорировать `venv/`, `.git/`, `__pycache__/`, `.pytest_cache/`, `.repowise/`, `.kilo/`, `bin/`, `app.db` и `openapi.json`.
