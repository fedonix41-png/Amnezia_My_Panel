import re

with open('telegram_bot.py', 'r') as f:
    content = f.read()

# Replace all occurrences of load_data_fn / save_data_fn
content = re.sub(r',\s*load_data_fn:\s*Callable', ', db', content)
content = re.sub(r',\s*save_data_fn:\s*Optional\[Callable\]\s*=\s*None', '', content)
content = re.sub(r',\s*save_data_fn:\s*Optional\[Callable\]', '', content)
content = re.sub(r',\s*load_data_fn', ', db', content)
content = re.sub(r',\s*save_data_fn', '', content)

# _build_connections_keyboard
old = """def _build_connections_keyboard(conns: list, data: dict) -> dict:
    \"\"\"Build inline keyboard where each button = one connection.\"\"\"
    rows = []
    servers = data.get("servers", [])
    for c in conns:
        sid = c.get("server_id", 0)
        server_name = "Unknown"
        if isinstance(sid, int) and sid < len(servers):
            srv = servers[sid]
            server_name = srv.get("name") or srv.get("host", "Unknown")[:20]
        proto = c.get("protocol", "").upper()
        name = c.get("name", "Connection")
        label = f"🔐 {name} · {proto} · {server_name}"
        rows.append([{"text": label, "callback_data": f"cfg:{c['id']}"}])
    rows.append([{"text": "🔄 Refresh list", "callback_data": "refresh"}])
    return {"inline_keyboard": rows}"""

new = """def _build_connections_keyboard(conns: list[models.Connection]) -> dict:
    \"\"\"Build inline keyboard where each button = one connection.\"\"\"
    rows = []
    for c in conns:
        server_name = "Unknown"
        if c.server:
            server_name = c.server.name or c.server.host[:20]
        proto = c.protocol.upper()
        name = c.name or "Connection"
        label = f"🔐 {name} · {proto} · {server_name}"
        rows.append([{"text": label, "callback_data": f"cfg:{c.id}"}])
    rows.append([{"text": "🔄 Refresh list", "callback_data": "refresh"}])
    return {"inline_keyboard": rows}"""
content = content.replace(old, new)

with open('telegram_bot.py', 'w') as f:
    f.write(content)
print("Done phase 2")
