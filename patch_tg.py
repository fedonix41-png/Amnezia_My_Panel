import re

with open('telegram_bot.py', 'r') as f:
    content = f.read()

def replace_func(name, new_code):
    global content
    # Find start of function
    start_idx = content.find(f"def {name}(")
    if start_idx == -1:
        start_idx = content.find(f"async def {name}(")
    if start_idx == -1:
        print(f"NOT FOUND: {name}")
        return
    # Find end of function
    lines = content[start_idx:].split("\n")
    end_idx = 0
    for i, line in enumerate(lines):
        if i == 0: continue
        if line and not line.startswith(" ") and not line.startswith("\t") and not line.startswith("#"):
            end_idx = i
            break
    if end_idx == 0:
        end_idx = len(lines)
    
    old_func = "\n".join(lines[:end_idx])
    content = content.replace(old_func, new_code + "\n")

replace_func('_user_label', '''def _user_label(user) -> str:
    label = user.username or str(user.id)
    role = user.role or "user"
    suffix = f" · {role}"
    if getattr(user, "telegramId", None):
        suffix += f" · tg:{user.telegramId}"
    if getattr(user, "enabled", True) is False:
        suffix += " · disabled"
    return f"{label}{suffix}"''')

replace_func('_users_keyboard', '''async def _users_keyboard(db, back_callback: str = "adm:menu") -> dict:
    rows = []
    rows.append([{"text": "➕ Create User", "callback_data": "adm:add_user"}])
    stmt = select(models.User).limit(40)
    res = await db.execute(stmt)
    for user in res.scalars().all():
        rows.append([{"text": f"👤 {_user_label(user)}", "callback_data": _ref("user", {"uid": user.id})}])
    rows.append([{"text": "⬅️ Back", "callback_data": back_callback}])
    return {"inline_keyboard": rows}''')

replace_func('_assign_user_keyboard', '''async def _assign_user_keyboard(db, server_id: int, proto: str, name: str) -> dict:
    rows = [[{"text": "🚫 Do not assign", "callback_data": _ref("create_client", {"sid": server_id, "proto": proto, "name": name, "user_id": None})}]]
    stmt = select(models.User).limit(40)
    res = await db.execute(stmt)
    for user in res.scalars().all():
        rows.append([{"text": f"👤 {_user_label(user)}", "callback_data": _ref("create_client", {"sid": server_id, "proto": proto, "name": name, "user_id": user.id})}])
    rows.append([{"text": "❌ Cancel", "callback_data": _ref("clients", {"sid": server_id, "proto": proto})}])
    return {"inline_keyboard": rows}''')

replace_func('_server_keyboard', '''async def _server_keyboard(db) -> dict:
    rows = []
    stmt = select(models.Server).order_by(models.Server.display_order.asc(), models.Server.id.asc())
    res = await db.execute(stmt)
    for srv in res.scalars().all():
        name = srv.name or srv.host or f"Server {srv.id}"
        rows.append([{"text": f"🖥 {name}", "callback_data": f"srv:{srv.id}"}])
    rows.append([{"text": "⬅️ Admin menu", "callback_data": "adm:menu"}])
    return {"inline_keyboard": rows}''')

with open('telegram_bot.py', 'w') as f:
    f.write(content)

