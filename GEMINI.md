# Antigravity Workspace — GEMINI.md

Project conventions live in `AGENTS.md`. This file covers codebase navigation, sub-agent registry, and Antigravity workflows.

## Codebase navigation

The core files are large. A practical approach:

- `app.py` (~2157 lines), `telegram_bot.py` (~1680 lines) — `grep` by function signature or decorator, then `read` with offset/limit. Reference as `file:line` in your notes.
- Deep context (data schema, protocol registry, security boundaries) — `docs/ARCHITECTURE.md`.
- When exploring unfamiliar territory, consider using a Read-Only sub-agent (`explore` / `research`) to avoid loading large files into the main context.

## Common entry points (grep by name, not line numbers)

- `_check_admin` — central authorization gate, signature: `_check_admin(db, request)`
- `get_ssh` — SSHManager factory (handles both dict and ORM model inputs)
- `get_protocol_manager` — protocol dispatcher with lazy imports
- `_manager_call` — unified manager method call (hides WireGuard special-case)
- `BASE_PROTOCOLS` / `MULTI_INSTANCE_PROTOCOLS` — protocol registry
- `ensure_docker_installed` — remote Docker installation
- SSH host key pinning: `ssh_manager.py` (`_PinningHostKeyPolicy`)
- `build_initial_server_json`: `xray_manager.py` (VLESS-Reality structure)
- Router mounting: `app.py` (`app.include_router`)
- Startup/lifespan: `app.py` (`_app_lifespan`, `_startup`)
- Database models: `database/models.py` (User, Server, ServerProtocol, UserConnection, ApiToken, Setting)
- Repositories: `database/repositories.py` (UserRepository, ServerRepository)

## Sub-agents

- **`explore` / `research`** (Read-Only) — codebase exploration, documentation research, external library lookups without polluting the main context.
- **`protocol-builder`** (Write) — new VPN/proxy protocol end-to-end: `managers/`, `routers/protocols.py`, `app.py`. Checklist: `docs/ARCHITECTURE.md` §13.
- **`security-auditor`** (Read-Only) — SSH (paramiko stdin), Fernet (`v1:`), `shlex.quote`, endpoint security review.
- **`refactor-auditor`** (Write) — refactoring with 100% backward compatibility and passing tests.

## Workflows

- Large autonomous tasks work well with the `/goal` flow — iterate until complete.
- Before major architectural changes, an `implementation_plan.md` in Planning Mode helps align on approach before writing code.
- Running `venv/bin/python -m pytest` after changes is the standard verification step. Tests use an in-memory SQLite database, so no external setup is needed.
