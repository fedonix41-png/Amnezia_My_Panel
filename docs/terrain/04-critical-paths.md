# Потоки данных и Карта зависимостей

> Связан: [INDEX](INDEX.md) | [03-infra](03-infrastructure.md) | [05-gotchas](05-gotchas.md)

## Поток 1: Установка протокола

```
POST /api/servers/{server_id}/install
  ↓ `routers/protocols.py` → endpoint install
  ↓ _check_admin(db, request)           → проверка роли (session/bearer)
  ↓ protocol_base(req.protocol)          → 'awg__2' → 'awg'
  ↓ next_protocol_key(existing, base)    → находит свободный instance#
  ↓ get_ssh(server)                     → SSHManager + Fernet decrypt
  ↓ ssh.connect()                       → paramiko handshake + pin fingerprint
  ↓ ensure_docker_installed(ssh)         → apt install docker on remote
  ↓ get_protocol_manager(ssh, protocol)  → deferred import + factory
  ↓ dispatch в `routers/protocols.py` по protocol_base
  │   WireGuard: manager.install_protocol(port=port)       ← БЕЗ protocol_type!
  │   AWG:       manager.install_protocol(protocol, port=port)
  │   Xray:      manager.install_protocol(port=port)
  │   Telemt:    manager.install_protocol(protocol_type=protocol, port=port, ...)
  ↓   → SSH: docker build/run, iptables, конфиги
  ↓ db.add(ServerProtocol(...))
  ↓ db.commit()
  ↓ ssh.disconnect()
```

## Поток 2: Добавление клиентского подключения

```
POST /api/servers/{server_id}/connections/add
  ↓ `routers/connections.py` → endpoint add connection
  ↓ _check_admin → get_ssh → ssh.connect()
  ↓ get_protocol_manager(ssh, protocol)
  ↓ Dispatch по protocol_base:
  │   telemt    → manager.add_client(protocol, name, host, port, **kwargs)
  │   wireguard → manager.add_client(name, host)     ← БЕЗ protocol_type!
  │   awg       → manager.add_client('awg', name, host, port)
  │   xray      → manager.add_client('xray', name, host, port)
  ↓ generate_vpn_link(config_text)       → vpn:// URI
  ↓ generate_qr_content(config_text)     → QR base64
  ↓ db.add(Connection(...))
  ↓ db.commit()
  ↓ ssh.disconnect()
```

## Поток 3: Telegram — установка протокола

```
Callback: r:<ref_key> → _resolve_ref() → action='do_install_proto'
  ↓ `telegram_bot.py` → `_admin_do_install_protocol()`
  ↓ async with get_db_context() as db    ← fresh session per update!
  ↓ ServerRepository.get_by_id(db, server_id)
  ↓ asyncio.to_thread(get_ssh_and_manager, ...)
  │   ↓ SSHManager.connect()
  │   ↓ ensure_docker_installed(ssh)
  │   ↓ manager.install_protocol(...)
  ↓ db.add(ServerProtocol(...))
  ↓ db.commit()
  ↓ edit_message()                       ← update inline keyboard
```

⚠️ SSH через `asyncio.to_thread()` — не блокирует polling.

## Поток 4: Share-ссылка (публичный доступ)

```
GET /share/{token}
  ↓ `routers/sharing.py` → endpoint share page
  ↓ UserRepository.get_by_share_token(db, token)
  ↓ Проверка: share_enabled=True
  ↓ Рендеринг user_share.html

POST /api/share/{token}/auth
  ↓ `routers/sharing.py` → endpoint share authentication
  ↓ verify_password(req.password, user.share_password_hash)
  ↓ session['share_user_id'] = user.id

GET /api/share/{token}/connections
  ↓ `routers/sharing.py` → endpoint list connections
  ↓ select(Connection).where(user_id=share_user.id)
  ↓ Только подключения владельца
```

## Граф зависимостей

```
config.py  ← (load_dotenv)
    ↑
    ├── middleware.py
    │       ↑
    │       └── app.py  (главный модуль)
    │               ├── top-level imports:
    │               │     ├── managers/{ssh,awg,xray,wireguard,backup}_manager
    │               │     ├── managers/{secrets,amnezia_config}
    │               │     └── telegram_bot
    │               │
    │               ├── deferred (в get_protocol_manager):
    │               │     └── managers/{telemt,dns,socks5,adguard,nginx}_manager
    │               │
    │               ├── database/{session,models,crud}
    │               │
    │               └── routers/* (9 модулей)
    │                     └── каждый: import app as app_module
    │
    ├── database/session.py
    │       └── database/models.py
    │
    └── database/repositories.py
            ├── database/models.py
            └── managers/secrets.py
```

### DI-контейнеры

**Отсутствуют.** Зависимости через:
- FastAPI `Depends(get_db)` — session per request
- Параметры функций — SSHManager передаётся явно
- Модульные глобалы — `posthog_client`, `limiter`, `templates`

### Циклический импорт (осознанный)

Алиас `sys.modules['app'] = sys.modules[__name__]` в `app.py` обеспечивает корректный импорт при запуске как `__main__`.
Роутеры импортируют `app` в начале файла, а `app` импортирует роутеры в секции `ROUTERS REGISTRATION`.
