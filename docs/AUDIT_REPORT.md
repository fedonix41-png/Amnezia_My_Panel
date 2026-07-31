# Сводный отчёт аудита — Amnezia Web Panel

**Дата:** 2026-07-31  
**Объём:** ~8000 строк кода в 30+ файлах  
**Метод:** параллельный аудит по 5 направлениям (аутентификация, БД, API-роуты, Telegram-бот, SSH-менеджеры)

---

## 🔴 КРИТИЧЕСКИЕ (P0) — неработающий код / уязвимости удалённого выполнения

### 1. Telegram-бот: ВСЕ SSH-операции проваливаются из-за нерасшифрованных кредов

**Файл:** `telegram_bot.py:426-432`, функция `_get_ssh_and_manager`

`_build_data_dict` сохраняет Fernet-зашифрованные `s.password` / `s.private_key` (формат `v1:<token>`). `_get_ssh_and_manager` передаёт их в `SSHManager` без дешифровки — SSH-подключение к серверам всегда падает.

**Затронутые функции:**
- `_send_config_by_client`
- `_handle_send_qr`
- `_handle_download_conf`
- `_admin_toggle_protocol`
- `_admin_clients`
- `_admin_create_client`
- `_admin_toggle_client`
- `_admin_remove_client`
- `_admin_do_uninstall_protocol`
- `_refresh_server_protocol_statuses`

**Fix:** Использовать `app.get_ssh(server)`, который вызывает `decrypt_secret()`:
```python
def _get_ssh_and_manager(server: dict, proto: str):
    from app import get_ssh, get_protocol_manager
    ssh = get_ssh(server)
    manager = get_protocol_manager(ssh, proto)
    return ssh, manager
```

---

### 2. Telegram-бот: ВСЕ DB-записи молча не работают

**Файл:** `telegram_bot.py:881, 938, 959, 987, 1177, 1234, 1382, 1445`

Повсеместно вызывается синхронный `db.commit()` на `AsyncSession` — либо падает с `MissingGreenlet`, либо возвращает корутину, которая отбрасывается. **Ни одно изменение (добавление/удаление пользователя, сервера, клиента) не сохраняется в БД.**

Плюс мутации делаются над in-memory dict `_build_data_dict`, а не ORM-объектами — даже при работающем commit SQL бы не генерировался.

**Fix:** Все мутации переписать на ORM через репозитории + `await db.commit()`:
```python
new_user = models.User(username=username, role=role, ...)
db.add(new_user)
await db.commit()
```

---

### 3. Telegram-бот: `/addserver` крашится с `AttributeError`

**Файл:** `telegram_bot.py:870-871`

`_build_data_dict` возвращает `data["servers"]` как `dict` (ключи — ID серверов). Команда делает `data.setdefault("servers", []).append(...)` — `dict` не имеет `.append()`, вызов падает. Плюс пароль сохраняется в открытом виде (без Fernet) и остаётся в истории Telegram-чата.

**Fix:** Переписать на ORM:
```python
server = models.Server(
    name=name, host=host, ssh_port=ssh_port,
    username=username, password=encrypt_secret(password)
)
db.add(server)
await db.commit()
```

---

### 4. Telegram-бот: `_admin_delete_user` разрушает структуру данных

**Файл:** `telegram_bot.py:986-987`

```python
data["users"] = [u for u in data.get("users", {}).values() if u.get("id") != user_id]
```

Конвертирует `data["users"]` из `dict` в `list`. Последующий код, ожидающий `.values()`, падает. Пользователь фактически не удаляется из БД.

**Fix:** Переписать на ORM.

---

### 5. API: `perform_delete_user` / `perform_toggle_user` — разбитые эндпоинты

**Файл:** `app.py:1159-1206` (функции) + `routers/users.py:259,274` (вызовы)

Сигнатура функций: `perform_delete_user(data: dict, user_id: str)`. Вызываются как:
```python
success = await app_module.perform_delete_user(db, user_id)
```

`AsyncSession` передаётся как `data` → `data['users']` вызывает `TypeError`. Удаление и блокировка пользователя через веб-панель возвращают 500.

**Fix:** Переписать на ORM либо маршрутизировать одиночные операции через `perform_mass_operations`.

---

### 6. API: `api_add_user` / `api_delete_user` / `api_toggle_user` — обход Bearer-токенов

**Файл:** `routers/users.py:122, 252, 270`

Используют `get_current_user(db, request)` (только сессионная кука) вместо `_check_admin()` (сессия + Bearer). API-токены игнорируются — администратор с Bearer-токеном не может управлять пользователями. Нарушает документированный контракт «Bearer-токены эквивалентны admin-сессии».

**Fix:** Заменить на:
```python
cur = await app_module._check_admin(db, request)
```

---

### 7. Command Injection в Telemt API curl

**Файл:** `managers/telemt_manager.py:56-57`

```python
js_data = json.dumps(data).replace('"', '\\"')
cmd += f" -H 'Content-Type: application/json' -d \"{js_data}\""
```

Экранирование ломаное — backticks, `$()`, переводы строк не экранируются. Поле `user_ad_tag` приходит из пользовательского ввода (`telemt_manager.py:386`) и попадает в JSON → shell injection.

**Fix:** `shlex.quote(js_data)` или запись JSON во временный файл:
```python
cmd += f" -d @/tmp/payload.json"
```

---

### 8. Command Injection в Docker-командах — порты не приводятся к int

**Файлы:**
- `managers/wireguard_manager.py:207` — `-p {port}:{port}/udp`
- `managers/awg_manager.py:422` — `-p {port}:{port}/udp`
- `managers/xray_manager.py:336` — `-p {port}:{port}/tcp`
- `managers/telemt_manager.py:200` — `re.sub(r'public_port\s*=\s*\d+', f'public_port = {port}', ...)`

Порты интерполируются напрямую в shell-команды без приведения к `int()`. Если роутер пропустит нечисловое значение, возможна инъекция.

**Сравнение:** `socks5_manager.py:153` и `nginx_manager.py:252` делают `int(port)` — остальные менеджеры должны делать так же.

**Fix:** Добавить в начало `install_protocol`:
```python
port = int(port or <default>)
```

---

### 9. Open Redirect через `/set_lang/{lang}`

**Файл:** `routers/templates.py:21-26`

```python
return RedirectResponse(url=request.headers.get("referer", "/"))
```

Редирект на заголовок `Referer` без валидации. Атакующий может отправить ссылку с `Referer: https://evil.com` — пользователь будет перенаправлен на фишинговый сайт.

**Fix:** Валидировать `Referer` против списка доверенных URL панели, либо всегда редиректить на `/`.

---

### 10. (Латентная) Command Injection в WireGuard/AWG peer section echo

**Файлы:** `managers/wireguard_manager.py:604-606`, `managers/awg_manager.py:1016-1018`

```python
escaped_peer = peer_section.replace("'", "'\\''")
f"docker exec -i {container_name} bash -c 'echo \"{escaped_peer}\" >> {config_path}'"
```

Экранируются только одинарные кавычки; double-quotes, backticks, `$()` — нет. Пока peer-секции генерируются кодом, но при добавлении пользовательских полей (имя клиента) станет активной уязвимостью.

**Fix:** Записывать конфиг через `docker cp` из временного файла через SFTP.

---

## 🟠 ВЫСОКИЕ (P1) — уязвимости, потеря данных, деградация

### Аутентификация и сессии

| # | Файл:строка | Проблема | Fix |
|---|-------------|----------|-----|
| H1 | `app.py:1154` | **Timing side-channel** при сравнении паролей: `new_h.hex() == h` вместо `hmac.compare_digest()` | `return hmac.compare_digest(new_h.hex(), h)` |
| H2 | `app.py:1146` | **PBKDF2 100k итераций** — OWASP 2023 требует 600k | Поднять до 600000, внедрить прозрачный re-hash |
| H3 | `app.py:105-110` | **Сессионная кука без max_age/ротации** — угнанная кука живёт вечно, logout не инвалидирует | Установить `max_age` (8h), ротировать сессию при логине |
| H4 | `config.py:125` | **Cookie Secure=false по умолчанию** — кука передаётся по HTTP | `SESSION_COOKIE_SECURE = True` в production |
| H5 | `config.py:158` | **Хардкод `secure_password`** выглядит как реальный кред | Заменить на `CHANGE_ME` или требовать `DATABASE_URL` |
| H6 | `repositories.py:92-93` | **Дефолтный пароль `admin`** без принудительной смены | Генерировать случайный пароль при первом запуске |

### База данных

| # | Файл:строка | Проблема | Fix |
|---|-------------|----------|-----|
| H7 | `models.py:119` | **Нет индекса на `ApiToken.token_hash`** — каждый Bearer-запрос делает full scan | `mapped_column(String(255), index=True)` |
| H8 | `migrations/.../3a0e53a92ebd...:24` | **Миграция сломается на PostgreSQL** — `nullable=False` без `server_default` | `server_default='0'` или split: nullable → populate → NOT NULL |
| H9 | `repositories.py:272-281` | **Race condition в SettingRepository** — SELECT-then-UPSERT | `INSERT ... ON CONFLICT DO UPDATE` |
| H10 | `models.py:27,39,104-105` | **Нет индексов** на `telegramId`, `share_token`, `client_id`, `protocol` | `index=True` на все lookup-колонки |
| H11 | `repositories.py:153-156` | **Пустой пароль ломает шифрование** — `if updates["password"]:` пропускает `""` | Различать «ключ отсутствует» и «ключ с пустым значением» |

### Telegram-бот

| # | Файл:строка | Проблема | Fix |
|---|-------------|----------|-----|
| H12 | `telegram_bot.py:844-882` | **Пароль сервера в открытом виде в Telegram-чате** | Убрать `/addserver` или принимать пароль в private-сообщении + `encrypt_secret()` |
| H13 | `telegram_bot.py:1084` | **`docker start/stop` без `shlex.quote`** для container name | `shlex.quote(str(container))` |
| H14 | `telegram_bot.py:700-705` | **Сырые exception-сообщения показываются пользователю** — утечка путей/хостнеймов | Логировать `logger.exception()`, пользователю — generic message |
| H15 | `telegram_bot.py:1455-1507` | **`_pending_inputs` никогда не очищается** — утечка памяти | Добавить 10-минутный таймаут |

### SSH и протоколы

| # | Файл:строка | Проблема | Fix |
|---|-------------|----------|-----|
| H16 | `nginx_manager.py:104-108` | **Валидация email — только проверка наличия `@`** | Использовать `email.utils` или жёсткий regex |
| H17 | `ssh_manager.py:97` | **Поддержка DSA/DSS ключей** (1024-bit, SHA-1) | Убрать DSSKey или логировать warning |
| H18 | `xray_manager.py:248` | **Загрузка Xray binary без проверки хэша/подписи** | Закрепить SHA256 хэш релиза |
| H19 | `ssh_manager.py:151-152` | **Неограниченное чтение stdout/stderr** — OOM | Ограничить чтение (1MB) |

### API и роутеры

| # | Файл:строка | Проблема | Fix |
|---|-------------|----------|-----|
| H20 | `routers/settings.py:327-333` | **Бэкап сливает `password_hash`, `share_token`, креды серверов** | Строить whitelist-словари вместо `to_dict()` |
| H21 | `routers/settings.py:347-353` | **DoS: восстановление бэкапа без лимита размера файла** | `config.MAX_UPLOAD_SIZE` или `request.stream()` |
| H22 | `routers/protocols.py:530-547` | **Stored XSS: HTML для NGINX-сайта без санитизации** | Strip `<script>`, `<iframe>`, event handlers |
| H23 | `routers/protocols.py:465-504` | **Сохранение сырых конфигов без валидации** | Валидировать структуру конфига перед записью |

---

## 🟡 СРЕДНИЕ (P2) — технический долг, best practices

### Общие

| # | Файл:строка | Проблема |
|---|-------------|----------|
| M1 | `app.py:1457` | Дублирование логики SHA-256 — должен использоваться `_hash_api_token()` |
| M2 | `app.py:1462-1463` | `last_used_at` пишется при КАЖДОМ Bearer-запросе без debounce |
| M3 | `app.py:1103-1141` | Мёртвый код `_resolve_api_token` / `_touch_api_token` (legacy dict-based) |
| M4 | `app.py:1469-1473` | `tpl()` грузит всю таблицу settings на каждый рендер (нужен кэш) |
| M5 | `middleware.py:37-38` | CSP разрешает `'unsafe-inline'` — отключает XSS-защиту |
| M6 | `auth.py:55` | Сравнение капчи case-insensitive — снижает энтропию |
| M7 | `session.py:19-24` | Нет `pool_size` / `max_overflow` для `asyncpg` |
| M8 | `repositories.py:87-102` | `ensure_default_admin` без `ORDER BY` возвращает произвольного пользователя |
| M9 | `models.py:134` | Алиас `Connection = UserConnection` путает grep и разработчиков |

### Telegram-бот

| # | Файл:строка | Проблема |
|---|-------------|----------|
| M10 | `telegram_bot.py:623-647` | Disabled-пользователи могут пользоваться ботом |
| M11 | `telegram_bot.py` | Нет rate-limiting на команды бота |
| M12 | `telegram_bot.py:250-259` | `_callback_refs` копит записи при низком трафике |

### API

| # | Файл:строка | Проблема |
|---|-------------|----------|
| M13 | `routers/servers.py:241-278` | Деструктивные действия (reboot, clear) без подтверждения |
| M14 | `routers/api_tokens.py:86-100` | Любой admin/support может отозвать чужие токены |
| M15 | `routers/connections.py:211-227` | `api_get_connection_config` — нет ownership check для admin/support |

### SSH и протоколы

| # | Файл:строка | Проблема |
|---|-------------|----------|
| M16 | `socks5_manager.py:103` | Пароли SOCKS5 в открытом виде в конфиге 3proxy |
| M17 | `wireguard_manager.py:621` | Приватные ключи клиентов хранятся на сервере без шифрования |
| M18 | `ssh_manager.py:68-113` | Нет TCP keepalive на SSH-соединениях |
| M19 | `managers/*` (множество) | Орфанные временные файлы при падении операций |
| M20 | `adguard_manager.py:241` | Broken admin URL при `expose_web=False` |

---

## 📋 Приоритетный порядок исправлений

### Блок 1 — Немедленно (блокирует production):

1. **Telemt command injection** — `shlex.quote` для JSON в curl (`telemt_manager.py:56`)
2. **Порты → int** в `wireguard/awg/xray/telemt install_protocol`
3. **Telegram: расшифровка кредов** — использовать `app.get_ssh()` (`telegram_bot.py:426`)
4. **Telegram: `await db.commit()` + ORM-мутации** — весь файл `telegram_bot.py`
5. **Open redirect** `/set_lang` (`routers/templates.py:21`)
6. **`perform_delete_user`/`perform_toggle_user`** — переписать на ORM (`app.py:1159`)
7. **Bearer-токены в `users.py`** — заменить на `_check_admin` (`routers/users.py:122,252,270`)

### Блок 2 — До production (безопасность):

8. **Timing-safe `verify_password`** — `hmac.compare_digest()` (`app.py:1154`)
9. **Сессионная кука** — `max_age` + ротация при логине (`app.py:105`)
10. **`SESSION_COOKIE_SECURE = True`** в production (`config.py:125`)
11. **Принудительная смена пароля `admin`** (`repositories.py:92`)
12. **CSP** — убрать `unsafe-inline` (рефакторинг шаблонов, `middleware.py:37`)
13. **Индексы** — `token_hash`, `telegramId`, `share_token`, `client_id`, `protocol`
14. **Race condition в SettingRepository** → `ON CONFLICT DO UPDATE` (`repositories.py:272`)
15. **Санитизация HTML** в NGINX site save (`routers/protocols.py:530`)
16. **Лимит размера файла** для restore backup (`routers/settings.py:347`)
17. **PBKDF2 600k итераций** — обновить KDF (`app.py:1146`)

### Блок 3 — После production (стабилизация):

18. **Telegram: rate-limiting, disabled-user check, очистка `_pending_inputs`**
19. **Конфигурация пула соединений `asyncpg`** — `pool_size`, `max_overflow` (`session.py:19`)
20. **Кэширование `tpl()` settings** — `functools.lru_cache` (`app.py:1469`)
21. **SSH keepalive** (`ssh_manager.py:68`)
22. **Верификация хэша Xray binary** (`xray_manager.py:248`)
23. **Уборка мёртвого кода** — `_resolve_api_token`, legacy dict-функции
24. **Email-валидация** — жёсткий regex (`nginx_manager.py:104`)
25. **Очистка временных файлов** — `try/finally` во всех менеджерах
26. **Подтверждение деструктивных действий** — reboot, clear, delete
27. **Ownership check для отзыва токенов** — только свои или только `admin`

---

## Положительные находки

Несмотря на перечисленные проблемы, кодовая база имеет сильные стороны:

- ✅ **Нет pattern `echo '<pass>' | sudo -S`** — sudo-пароли передаются через paramiko stdin (безопасно)
- ✅ **Host key pinning** — `_PinningHostKeyPolicy`, не `AutoAddPolicy`
- ✅ **Fernet-шифрование кредов серверов** — `MASTER_KEY` обязателен при запуске
- ✅ **Нет сырых SQL-запросов** — всё через SQLAlchemy ORM с параметризацией
- ✅ **`shlex.quote`** используется в `backup_manager`, `ssh_manager`, `nginx_manager`, `xray_manager`
- ✅ **Домен-валидация** в `nginx_manager` через regex
- ✅ **SFTP-based загрузка файлов** с безопасными временными путями
