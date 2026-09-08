---
name: "python-uv-workflow"
description: "Активировать при установке, обновлении или удалении пакетов Python, а также при настройке виртуального окружения."
---
# Управление зависимостями Python

1. **Окружение**: Проект использует `venv/` + `requirements.txt`. Весь Python — через `venv/bin/python`, `venv/bin/pip`.
2. **Запреты**: Не вводить `uv`, `poetry` или другие пакетные менеджеры.
3. **Синтаксис**:
   - Установка: `venv/bin/pip install <package>`
   - Фиксация: добавить версию в `requirements.txt`
   - Проверка: `venv/bin/python -m pytest`