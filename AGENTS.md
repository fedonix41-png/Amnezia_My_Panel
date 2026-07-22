# Amnezia Web Panel

Multi-protocol VPN web panel. FastAPI ASGI server managing WireGuard, AmneziaWG, Xray (VLESS-Reality), Socks5, AdGuard, Nginx, DNS, and Telegram bot — deployed via Docker Compose with optional Cloudflare/ngrok tunnel.

## Stack

- **Python 3.14**, **FastAPI** (Starlette), **uvicorn**
- **Environment**: project `venv/` is mandatory — `venv/bin/python`, `venv/bin/pytest`. System Python will fail with `ModuleNotFoundError`.
- **Deployment**: Docker Compose (non-root, read-only rootfs, `cap_drop ALL`) + `docker-entrypoint.sh`. Dev mode: `AWP_DEV=1`.

## Commands

```bash
venv/bin/python -m pytest                          # Full test suite (pytest-asyncio, httpx)
venv/bin/python -m py_compile <file>               # Syntax check (no linter configured)
AWP_DEV=1 docker compose up                        # Local dev server
```

## Project Structure

| Directory/File | Responsibility |
|---|---|
| `app.py` | Core (~2233 lines): data loading, SSH factory, middleware, router mounting, protocol registry |
| `routers/` | FastAPI APIRouter modules: auth, templates, servers, protocols, connections, users, sharing, settings, api_tokens |
| `managers/` | One manager per protocol (duck-typed, no base class): ssh, secrets, awg, wireguard, xray, telemt, dns, adguard, nginx, socks5, backup |
| `config.py` | Env config: `SECRET_KEY`, `MASTER_KEY`, `AWP_DEV`, rate limits |
| `middleware.py` | CSRF double-submit, SecurityHeaders |
| `tests/` | pytest + pytest-asyncio; `conftest.py` at root |

Deep architecture — `docs/ARCHITECTURE.md`.

## Code Conventions

- Protocol managers: duck-typed interface — client methods take `protocol_type` as first argument. No base class. Add new protocol in `BASE_PROTOCOLS` (`app.py:902`), `get_protocol_manager` (`app.py:999`), name/container maps, install branch in `routers/protocols.py`. Full 7-step checklist: `docs/ARCHITECTURE.md` §13.
- No typecheck/lint tooling configured — `py_compile` is the only guard.
- `telegram_bot.py` is ~51KB — use `grep` to locate handlers before reading sections.

## Security

- All secrets in `data.json` stored via Fernet encryption (`managers/secrets.py`, format `v1:`). SSH credentials are `v1:`-encrypted.
- `SECRET_KEY` and `MASTER_KEY` mandatory from env. Only `AWP_DEV=1` skips this check.
- SSH passwords sent via **paramiko stdin channel**, never `echo '<p>' | sudo -S` (would leak to `/proc/<pid>/cmdline`).
- User-supplied strings (domain/email/username) validate before interpolation into sudo commands (cf. `nginx_manager._validate_domain`/`_validate_email`). Paths: `shlex.quote`.
- SSH host key pinning on first successful handshake (`ssh_manager.py:104-114`).

## Boundaries — Never Modify

- `.env` files and `data.json` — contain secrets, never commit (in `.gitignore`)
- System-level Python/pip packages — all work in project `venv/` only
- Generated/third-party: `venv/`, `__pycache__/`, `*.local`
