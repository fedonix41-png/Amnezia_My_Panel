# Amnezia Web Panel

Multi-protocol VPN web panel. FastAPI ASGI server managing WireGuard, AmneziaWG, Xray (VLESS-Reality), Socks5, AdGuard, Nginx, DNS, and Telegram bot — deployed via Docker Compose with optional Cloudflare/ngrok tunnel.

## Stack

- **Python 3.12+**, **FastAPI** (Starlette), **uvicorn**
- **Database**: SQLAlchemy (async) + PostgreSQL (production) / SQLite (dev). `data.json` is legacy — no longer the primary data store.
- **Environment**: the project works inside `venv/` — use `venv/bin/python`, `venv/bin/pytest`, `venv/bin/pip`. System Python won't have the project dependencies.
- **Deployment**: Docker Compose (non-root, read-only rootfs, `cap_drop ALL`) + `docker-entrypoint.sh`. Dev mode: `AWP_DEV=1`.

## Commands

```bash
venv/bin/python -m pytest                          # Full test suite (pytest-asyncio, httpx)
venv/bin/python -m py_compile <file>               # Syntax check
AWP_DEV=1 docker compose up                        # Local dev server
```

## Project Structure

| Directory/File | Responsibility |
|---|---|
| `app.py` | Core (~2157 lines): SSH factory, middleware, router mounting, protocol registry, tunnel management, WARP, API tokens, auth helpers |
| `routers/` | FastAPI APIRouter modules: auth, templates, servers, protocols, connections, users, sharing, settings, api_tokens |
| `managers/` | One manager per protocol (duck-typed, no base class): ssh, secrets, awg, wireguard, xray, telemt, dns, adguard, nginx, socks5, backup |
| `database/` | SQLAlchemy ORM: `models.py` (User, Server, ServerProtocol, UserConnection, ApiToken, Setting), `repositories.py`, `session.py`, `crud.py` |
| `config.py` | Env config: `SECRET_KEY`, `MASTER_KEY`, `AWP_DEV`, `DATABASE_URL`, rate limits |
| `middleware.py` | CSRF double-submit, SecurityHeaders |
| `tests/` | pytest + pytest-asyncio; `conftest.py` at root |

Deep architecture — `docs/ARCHITECTURE.md`.

## Code Conventions

- **Protocol managers**: duck-typed, no base class. Client methods take `protocol_type` as first argument — except `WireGuardManager` (single instance) whose methods don't. The helper `_manager_call(manager, method, protocol, *args)` papers over this difference. Adding a new protocol: 7-step checklist in `docs/ARCHITECTURE.md` §13.
- **Authorization**: `_check_admin(db, request)` — central gate, takes `AsyncSession` and `Request`, returns user or `None`. Roles: `admin`, `support`, `user`.
- The project has no type checker or linter configured. `py_compile` is the only automated guard before tests.
- `telegram_bot.py` (~1680 lines) — handlers can be located with `grep` by function name or callback pattern.

## Security

- **Data at rest**: SQLAlchemy models in `database/models.py`. SSH credentials (`Server.password`, `Server.private_key`) are `v1:`-encrypted via Fernet (`managers/secrets.py`). User passwords use PBKDF2-SHA256 (100k iterations). API tokens stored as SHA-256 hashes.
- `SECRET_KEY` and `MASTER_KEY` come from env. In dev mode (`AWP_DEV=1`) ephemeral keys are synthesized so you don't need to configure anything.
- **SSH sudo passwords** go through paramiko's stdin channel — never via `echo '<p>' | sudo -S` (leaks to `/proc/<pid>/cmdline`).
- **User-supplied strings** (domain, email, username) are validated before interpolation into shell commands: `nginx_manager._validate_domain` / `_validate_email`. File paths go through `shlex.quote`.
- **SSH host key pinning** on first successful handshake — `_PinningHostKeyPolicy`, not `AutoAddPolicy`.

## Files outside the project

These paths are never committed and are not part of the codebase:

- `.env` — secrets (in `.gitignore`)
- `venv/`, `__pycache__/`, `*.local`, `app.db` — generated at runtime
- System-level Python packages — all dependencies live in `venv/`
