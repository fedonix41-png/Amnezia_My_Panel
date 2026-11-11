---
name: "fernet-secrets-security"
description: "Активировать при работе с паролями, SSH-ключами, Fernet-шифрованием, API-токенами или CSRF/сессиями."
---
# Безопасность и Шифрование

1. **Секреты серверов**: `password` и `private_key` в модели `Server` шифруются алгоритмом Fernet (ключ `MASTER_KEY`, префикс `"v1:<fernet-token>"`) через `managers/secrets.py`.
2. **Пароли пользователей**: Используется строго PBKDF2-SHA256 на **600 000 итераций** (`hash_password` / `verify_password` в `app.py` и `database/repositories.py`, формат `iterations$salt$hash`). `bcrypt` не используется.
3. **API-токены**: Имеют префикс `awp_` и хранятся в таблице `api_tokens` **только как SHA-256 дайджесты** (`_hash_api_token`). Сырое значение возвращается один раз при создании.
4. **CSRF**: Все мутирующие методы (`POST/PUT/PATCH/DELETE`) защищены `CSRFMiddleware` (освобождены только запросы с `Authorization: Bearer awp_...` и явно заданные exempt-пути).
5. **Глубокий контекст (читать по требованию)**:
   - `docs/terrain/02-core.md` — криптография, `MASTER_KEY`, `SECRET_KEY`.
   - `docs/terrain/01-entry-points.md` — аутентификация и middleware.
