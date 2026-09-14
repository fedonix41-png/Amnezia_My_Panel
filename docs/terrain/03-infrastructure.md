# Инфраструктура

> Связан: [INDEX](INDEX.md) | [02-core](02-core.md) | [04-paths](04-critical-paths.md)

## База данных

| Параметр | Значение | Файл |
|---|---|---|
| Движок | `create_async_engine` | `database/session.py` |
| Dev | `sqlite+aiosqlite:///app.db` | `config.py` |
| Prod | `postgresql+asyncpg://...` | `config.py` |
| `pool_pre_ping` | `True` | `database/session.py` |
| asyncpg timeout | 5с | `database/session.py` |
| `expire_on_commit` | `False` | `database/session.py` |
| `autoflush` | `False` | `database/session.py` |
| Миграции | Alembic async, 3 ревизии | `migrations/versions/` |

**Доступ:**
- В роутах: `db: AsyncSession = Depends(get_db)` — per-request
- Вне роутов: `async with get_db_context() as session:` — context manager
- Коммит **всегда явный**: `await db.commit()`

**Репозитории:**
- `UserRepository` — CRUD + `ensure_default_admin()` (`repositories.py`)
- `ServerRepository` — CRUD с **авто-шифрованием** password/private_key при create/update
- `crud.py` — `get_setting(db, key)` / `set_setting(db, key, value)` для key-value JSON

## SSH (managers/ssh_manager.py, 285 строк)

**Единственный модуль, открывающий SSH-сокеты.** Все менеджеры получают `SSHManager` через конструктор.

| Метод | Timeout | Сайд-эффект |
|---|---|---|
| `connect()` | 15с | SSH-handshake, pin fingerprint, retry 4 key types |
| `run_command(cmd, timeout=60, stdin_data=None)` | 60с | Remote exec |
| `run_sudo_command(cmd, timeout=60)` | 60с | `sudo -S -p ''` + stdin password |
| `run_sudo_script(script, timeout=120)` | 120с | SFTP → `sudo bash` → rm |
| `upload_file_sudo(content, path)` | — | /tmp write → sudo mv → chmod |
| `test_connection()` | — | `uname -sr && cat /etc/os-release` |

**Безопасность:**
- Sudo password через **stdin-канал** (НЕ `echo | sudo -S`)
- `_PinningHostKeyPolicy` — fingerprint pin после первого handshake, reject на mismatch
- `on_fingerprint` callback при изменении отпечатка
- **Нет retry** — одна попытка, без повторов
- `allow_agent=False`, `look_for_keys=False`

Конструктор: `get_ssh(server, db)` в `app.py` — принимает dict или ORM `Server`, дешифрует credentials.

## Туннели (Cloudflare / ngrok)

| Компонент | Расположение | Описание |
|---|---|---|
| `TUNNEL_RUNTIMES` | `app.py` | `dict[str, TunnelRuntime]` — process, pid, url |
| `TUNNEL_LOCK` | `app.py` | `threading.Lock` — защита мутаций |
| `start_tunnel()` | `app.py` | `subprocess.Popen` + watcher thread |
| `stop_tunnel()` | `app.py` | Kill process + cleanup |
| `watch_tunnel_output()` | `app.py` | Background thread читает stdout |
| URL parsing | `app.py` | Regex `https://[^\s"\']+` |
| ngrok API | `app.py` | `127.0.0.1:4040` для public URL |
| State file | `tunnels_state.json` | Persistent tunnel config |

## WARP (Cloudflare)

| Функция | Расположение | Timeout | Описание |
|---|---|---|---|
| `run_warp_cli(*args)` | `app.py` | 12с | Subprocess wrapper |
| `enable_warp()` | `app.py` | — | Register + connect |
| `disable_warp()` | `app.py` | — | Disconnect |
| `get_warp_status()` | `app.py` | — | Full status dict |

## Фоновые задачи

| Задача | Цикл | Что делает |
|---|---|---|
| `_bg_traffic` (`periodic_background_tasks`, `:1911`) | `sleep(60)` → работа → `sleep(600)` | Трафик-синк, лимиты, expiration, Remnawave sync |
| `_bg_backup` (`_background_backup`, `:1877`) | `BACKUP_INTERVAL_HOURS` (по умолч. 6ч); `0` — отключено | Копия `DATA_FILE` с timestamps, pruning по `BACKUP_KEEP_COUNT` |

`periodic_background_tasks` также:
- `_scrape_server_traffic()` — **не** `wg show all`: перебирает протоколы `['awg','awg2','awg_legacy','xray','telemt','wireguard']`, для каждого вызывает `get_clients` и агрегирует `dataReceivedBytes` + `dataSentBytes`. Добавление нового протокола требует обновления этого списка.
- Сброс трафика по `User.traffic_reset_strategy` (`daily`/`weekly`/`monthly`/`never`) — сравнивает `last_reset_at` с текущим временем.
- Проверяет лимиты трафика → автоотключение
- Проверяет `expiration_date` → автоотключение
- Синхронизирует с Remnawave API (если настроен)

## PostHog

| Компонент | Описание |
|---|---|
| `posthog_client` | Global в `app.py`, `atexit` shutdown |
| Middleware | `PostHogRequestContextMiddleware` в `middleware.py` |
| Events | `user_logged_in`, через `capture_posthog_event()` |
| Режим | Опционален: клиент создаётся только при наличии `POSTHOG_PROJECT_TOKEN` и `POSTHOG_HOST` |

## Protocol Managers — Docker

| Протокол | Container | Image | Порт |
|---|---|---|---|
| `wireguard` | `amnezia-wireguard` | `amneziavpn/amnezia-wg:latest` (built) | 51820/UDP |
| `awg`, `awg2` | `amnezia-awg[-N]` | `amneziavpn/amneziawg-go:latest` | 55424/UDP |
| `awg_legacy` | `amnezia-awg-legacy` | `amneziavpn/amnezia-wg:latest` | 55424/UDP |
| `xray` | `amnezia-xray[-N]` | `alpine:3.15` + Xray v26 (built) | 443 TCP+UDP |
| `telemt` | `telemt[-N]` | Локальная сборка (docker compose) | 443, 9091 |
| `socks5` | `amnezia-socks5proxy[-N]` | `3proxy/3proxy:0.9.5` | 38080 |
| `adguard` | `amnezia-adguard` | `adguard/adguardhome:latest` | 53, 3000 |
| `nginx` | `amnezia-nginx` | `nginx:alpine` + certbot | 80, 443 |
| `dns` | `amnezia-dns` | `mvance/unbound:latest` | 53/UDP, 853 |

Docker-сеть `amnezia-dns-net` (subnet `172.29.172.0/24`) — создаётся dns/wg/awg менеджерами.

### Межменеджерные зависимости (через Docker, не Python)

- WG/AWG проверяют `amnezia-dns` → DNS = `172.29.172.254`
- AdGuard replace: стопит `amnezia-dns`, занимает `.254`
- AdGuard sidebyside: IP `.253` рядом с DNS
- DNS/AdGuard подключают VPN-контейнеры к `amnezia-dns-net`

### Паттерны ошибок (несогласованные!)

| Менеджер | При ошибке |
|---|---|
| WG, AWG, Xray | `raise RuntimeError` с `docker logs` |
| SOCKS5, AdGuard, DNS, Nginx, Backup | `return {'status': 'error', ...}` |
| Telemt | API-ошибки **молча** → `None` ⚠️ |
