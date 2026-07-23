import re

with open('telegram_bot.py', 'r') as f:
    content = f.read()

# Let's inject async session to all handlers.
# All handlers are called from `_dispatch`.

inject_db = """
from database.session import async_session
from database import models
from sqlalchemy import select
from sqlalchemy.orm import selectinload

async def _build_data_dict(db):
    data = {"users": [], "servers": [], "user_connections": []}
    
    # users
    res = await db.execute(select(models.User))
    for u in res.scalars().all():
        data["users"].append({
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "telegramId": u.telegramId,
            "enabled": u.enabled,
        })
        
    # servers
    res = await db.execute(select(models.Server).options(selectinload(models.Server.protocols)))
    for s in res.scalars().all():
        srv = {
            "id": s.id,
            "name": s.name,
            "host": s.host,
            "ssh_port": s.ssh_port,
            "username": s.username,
            "password": s.password,
            "private_key": s.private_key,
            "protocols": {}
        }
        for p in s.protocols:
            srv["protocols"][p.protocol_type] = {
                "installed": p.installed,
                "port": p.port,
                "base_protocol": p.base_protocol,
                "instance": p.instance,
                "display_name": p.display_name,
                "container_name": p.container_name,
                **(p.extra_params or {})
            }
        data["servers"].append(srv)
        
    # user_connections
    res = await db.execute(select(models.UserConnection))
    for c in res.scalars().all():
        data["user_connections"].append({
            "id": c.id,
            "user_id": c.user_id,
            "server_id": c.server_id,
            "protocol": c.protocol,
            "client_id": c.client_id,
            "name": c.name,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "last_bytes": c.last_bytes,
        })
        
    return data

"""

# We need to prepend this.
content = inject_db + content

# Replace load_data_fn() with await _build_data_dict(db)
# But wait, load_data_fn is called synchronously in many places!
# Handlers are async, so they can use await.
content = re.sub(r'data\s*=\s*load_data_fn\(\)', 'data = await _build_data_dict(db)', content)
content = re.sub(r'panel_user\s*=\s*_find_user\(load_data_fn,\s*tg_id\)', 'panel_user = await _find_user(db, tg_id)', content)
content = re.sub(r'if\s+not\s+_require_admin\(load_data_fn,\s*tg_id\):', 'if not await _require_admin(db, tg_id):', content)
content = re.sub(r',\s*load_data_fn:\s*Callable', ', db', content)
content = re.sub(r',\s*load_data_fn', ', db', content)

# Update _find_user to be async
content = content.replace("def _find_user(db, tg_id: str):", "async def _find_user(db, tg_id: str):")
content = content.replace("    data = await _build_data_dict(db)\n    tg_id_clean", "    data = await _build_data_dict(db)\n    tg_id_clean")

# Update _require_admin to be async
content = content.replace("def _require_admin(db, tg_id: str):", "async def _require_admin(db, tg_id: str):")
content = content.replace("    user = _find_user(db, tg_id)", "    user = await _find_user(db, tg_id)")

# _run_bot signature
content = content.replace("async def _run_bot(token: str, db, generate_vpn_link_fn: Callable):", "async def _run_bot(token: str, generate_vpn_link_fn: Callable):")

# Inside _dispatch
# We need to inject `async with async_session() as db:` around the dispatch body?
# Actually just inject it inside `_dispatch`.
old_dispatch = """async def _dispatch(api: TelegramAPI, update: dict, generate_vpn_link_fn: Callable):"""
new_dispatch = """async def _dispatch(api: TelegramAPI, update: dict, generate_vpn_link_fn: Callable):
    async with async_session() as db:
        await _dispatch_inner(api, update, db, generate_vpn_link_fn)

async def _dispatch_inner(api: TelegramAPI, update: dict, db, generate_vpn_link_fn: Callable):"""
content = content.replace(old_dispatch, new_dispatch)

with open('telegram_bot.py', 'w') as f:
    f.write(content)
print("Done")
