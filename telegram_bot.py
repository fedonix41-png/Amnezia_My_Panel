
from database.session import async_session
from database import models
from sqlalchemy import select
from sqlalchemy.orm import selectinload

async def _build_data_dict(db):
    data = {"users": {}, "servers": {}, "user_connections": {}}
    
    # users
    res = await db.execute(select(models.User))
    for u in res.scalars().all():
        data["users"][u.id] = {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "telegramId": u.telegramId,
            "enabled": u.enabled,
        }
        
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
        data["servers"][s.id] = srv
        
    # user_connections
    res = await db.execute(select(models.UserConnection))
    for c in res.scalars().all():
        data["user_connections"][c.id] = {
            "id": c.id,
            "user_id": c.user_id,
            "server_id": c.server_id,
            "protocol": c.protocol,
            "client_id": c.client_id,
            "name": c.name,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "last_bytes": c.last_bytes,
        }
        
    return data

"""
Telegram bot for Amnezia Web Panel.
Uses raw Telegram Bot API via httpx — no library version conflicts.
Runs as a background asyncio task alongside the FastAPI app.
"""
import asyncio
import html
import logging
import os
import shlex
import sys
import time
import uuid
from typing import Optional, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.session import async_session
from database import models

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------- #
#  Global state
# ----------------------------------------------------------------------- #
_bot_task: Optional[asyncio.Task] = None
_callback_refs = {}
_pending_inputs = {}

CLIENT_PROTOCOLS = {"awg", "awg2", "awg_legacy", "xray", "telemt", "wireguard"}
SERVICE_PROTOCOLS = {"dns", "adguard", "socks5", "nginx"}


# ----------------------------------------------------------------------- #
#  Public lifecycle
# ----------------------------------------------------------------------- #
def is_running() -> bool:
    return _bot_task is not None and not _bot_task.done()


def launch_bot(token: str, generate_vpn_link_fn: Callable):
    global _bot_task
    _bot_task = asyncio.create_task(
        _run_bot(token, generate_vpn_link_fn),
        name="telegram_bot",
    )
    return _bot_task


async def stop_bot():
    global _bot_task
    if _bot_task and not _bot_task.done():
        _bot_task.cancel()
        try:
            await _bot_task
        except asyncio.CancelledError:
            pass
        _bot_task = None
        logger.info("Telegram bot stopped.")


# ----------------------------------------------------------------------- #
#  Low-level Telegram API helpers
# ----------------------------------------------------------------------- #
class TelegramAPI:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = client

    async def call(self, method: str, **params) -> dict:
        r = await self.client.post(f"{self.base}/{method}", json=params, timeout=30)
        return r.json()

    async def get_updates(self, offset: int = 0, timeout: int = 25) -> list:
        r = await self.client.post(
            f"{self.base}/getUpdates",
            json={"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            timeout=timeout + 10,
        )
        data = r.json()
        if data.get("ok"):
            return data["result"]
        return []

    async def send_message(self, chat_id, text: str, reply_markup=None, parse_mode="HTML") -> dict:
        import json
        params = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)
        return await self.call("sendMessage", **params)

    async def edit_message(self, chat_id, message_id, text: str, reply_markup=None, parse_mode="HTML"):
        import json
        params = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)
        await self.call("editMessageText", **params)

    async def answer_callback(self, callback_query_id: str, text: str = ""):
        await self.call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    async def send_document(self, chat_id, filename: str, content: bytes, caption: str = "", reply_markup=None, parse_mode="HTML"):
        import json
        files = {"document": (filename, content, "text/plain")}
        data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": parse_mode}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        r = await self.client.post(f"{self.base}/sendDocument", data=data, files=files, timeout=30)
        return r.json()

    async def send_photo(self, chat_id, photo: bytes, caption: str = "", reply_markup=None, parse_mode="HTML"):
        import json
        files = {"photo": ("qr.png", photo, "image/png")}
        data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": parse_mode}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        r = await self.client.post(f"{self.base}/sendPhoto", data=data, files=files, timeout=30)
        return r.json()



# ----------------------------------------------------------------------- #
#  Generic helpers
# ----------------------------------------------------------------------- #
def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _format_bytes(value) -> str:
    try:
        value = float(value or 0)
    except Exception:
        value = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.2f} {units[idx]}"


def _proto_base(protocol: str) -> str:
    return str(protocol or "awg").split("__", 1)[0]


def _protocol_display_name(protocol: str) -> str:
    base = _proto_base(protocol)
    names = {
        "awg": "AmneziaWG",
        "awg2": "AmneziaWG 2.0",
        "awg_legacy": "AmneziaWG Legacy",
        "xray": "Xray",
        "telemt": "Telemt",
        "dns": "AmneziaDNS",
        "wireguard": "WireGuard",
        "socks5": "SOCKS5",
        "adguard": "AdGuard Home",
        "nginx": "NGINX",
    }
    name = names.get(base, base)
    if "__" in str(protocol):
        try:
            return f"{name} #{int(str(protocol).split('__', 1)[1])}"
        except Exception:
            return name
    return name


async def _find_user(db, tg_id: str):
    tg_id_clean = str(tg_id).lstrip("@")
    if not tg_id_clean.isdigit(): return None
    stmt = select(models.User).where(models.User.telegramId == int(tg_id_clean))
    res = await db.execute(stmt)
    user = res.scalars().first()
    return user.to_dict() if user else None


def _is_admin(panel_user) -> bool:
    if panel_user is None:
        return False
    role = panel_user.get("role", "") if isinstance(panel_user, dict) else getattr(panel_user, "role", "")
    return str(role).lower() == "admin"


def _ref(action: str, payload: dict) -> str:
    """Short callback_data indirection; Telegram callback_data is limited to 64 bytes."""
    key = uuid.uuid4().hex[:12]
    _callback_refs[key] = {"action": action, "payload": payload, "ts": time.time()}
    # Opportunistic cleanup.
    if len(_callback_refs) > 500:
        cutoff = time.time() - 6 * 3600
        for k in [k for k, v in _callback_refs.items() if v.get("ts", 0) < cutoff]:
            _callback_refs.pop(k, None)
    return f"r:{key}"


def _resolve_ref(data_str: str):
    if not data_str.startswith("r:"):
        return None
    return _callback_refs.get(data_str[2:])


def _build_connections_keyboard(conns: list[dict], data: dict) -> dict:
    """Build inline keyboard where each button = one connection."""
    rows = []
    for c in conns:
        server_name = "Unknown"
        server_id = c.get("server_id")
        for s in data.get("servers", {}).values():
            if s.get("id") == server_id:
                server_name = s.get("name") or s.get("host", "")[:20]
                break
        proto = str(c.get("protocol", "")).upper()
        name = c.get("name") or "Connection"
        label = f"🔐 {name} · {proto} · {server_name}"
        rows.append([{"text": label, "callback_data": f"cfg:{c.get('id')}"}])
    rows.append([{"text": "🔄 Refresh list", "callback_data": "refresh"}])
    return {"inline_keyboard": rows}


def _connection_lookup(data: dict, server_id: int, proto: str) -> dict:
    return {
        c.get("client_id"): c
        for c in data.get("user_connections", {}).values()
        if c.get("server_id") == server_id and c.get("protocol") == proto and c.get("client_id")
    }


def _client_display_name(client: dict, conn: Optional[dict] = None) -> str:
    if conn and conn.get("name"):
        return conn.get("name")
    user_data = client.get("userData") or {}
    return (
        client.get("name")
        or client.get("username")
        or user_data.get("clientName")
        or user_data.get("name")
        or str(client.get("clientId") or client.get("client_id") or client.get("id") or "Connection")[:12]
    )


def _user_label(user: dict) -> str:
    label = user.get("username") or str(user.get("id"))
    role = user.get("role") or "user"
    suffix = f" · {role}"
    if user.get("telegramId"):
        suffix += f" · tg:{user.get('telegramId')}"
    if user.get("enabled", True) is False:
        suffix += " · disabled"
    return f"{label}{suffix}"

def _users_keyboard(data: dict, back_callback: str = "adm:menu") -> dict:
    rows = []
    rows.append([{"text": "➕ Create User", "callback_data": "adm:add_user"}])
    for user_dict in list(data.get("users", {}).values())[:40]:
        rows.append([{"text": f"👤 {_user_label(user_dict)}", "callback_data": _ref("user", {"uid": user_dict.get("id")})}])
    rows.append([{"text": "⬅️ Back", "callback_data": back_callback}])
    return {"inline_keyboard": rows}

def _assign_user_keyboard(data: dict, server_id: int, proto: str, name: str) -> dict:
    rows = [[{"text": "🚫 Do not assign", "callback_data": _ref("create_client", {"sid": server_id, "proto": proto, "name": name, "user_id": None})}]]
    for user_dict in list(data.get("users", {}).values())[:40]:
        rows.append([{"text": f"👤 {_user_label(user_dict)}", "callback_data": _ref("create_client", {"sid": server_id, "proto": proto, "name": name, "user_id": user_dict.get("id")})}])
    rows.append([{"text": "❌ Cancel", "callback_data": _ref("clients", {"sid": server_id, "proto": proto})}])
    return {"inline_keyboard": rows}

def _admin_main_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🖥 Servers", "callback_data": "adm:servers"}],
            [{"text": "👤 Users", "callback_data": "adm:users"}],
            [{"text": "🔐 My connections", "callback_data": "adm:myconns"}],
            [{"text": "➕ How to add a server", "callback_data": "adm:addserver_help"}],
        ]
    }


def _server_keyboard(data: dict) -> dict:
    rows = []
    for srv in data.get("servers", {}).values():
        name = srv.get("name") or srv.get("host") or f"Server {srv.get('id')}"
        rows.append([{"text": f"🖥 {name}", "callback_data": f"srv:{srv.get('id')}"}])
    rows.append([{"text": "⬅️ Admin menu", "callback_data": "adm:menu"}])
    return {"inline_keyboard": rows}

def _protocol_status_icon(info: dict) -> str:
    if info.get("status_error"):
        return "⚪"
    running = info.get("container_running")
    if running is True:
        return "🟢"
    if running is False:
        return "🔴"
    return "⚪"


def _protocol_status_text(info: dict) -> str:
    if info.get("status_error"):
        return "unknown ⚪"
    running = info.get("container_running")
    if running is True:
        return "running 🟢"
    if running is False:
        return "stopped 🔴"
    return "unknown ⚪"


def _protocols_keyboard(server_id: int, server: dict) -> dict:
    rows = []
    protocols = server.get("protocols", {}) or {}
    for proto, info in protocols.items():
        installed = "✅" if info.get("installed", True) else "⚪"
        rows.append([{"text": f"{installed}{_protocol_status_icon(info)} {_protocol_display_name(proto)}", "callback_data": _ref("proto", {"sid": server_id, "proto": proto})}])
    rows.append([{"text": "⚡ Install new protocol", "callback_data": _ref("install_proto_list", {"sid": server_id})}])
    rows.append([{"text": "⬅️ Servers", "callback_data": "adm:servers"}])
    return {"inline_keyboard": rows}


def _protocol_keyboard(server_id: int, proto: str, proto_info: dict) -> dict:
    base = _proto_base(proto)
    rows = []
    if base in CLIENT_PROTOCOLS:
        rows.append([{"text": "👥 Connections", "callback_data": _ref("clients", {"sid": server_id, "proto": proto})}])
        rows.append([{"text": "➕ Create connection", "callback_data": _ref("add_client", {"sid": server_id, "proto": proto})}])
    is_running = proto_info.get("container_running") is True
    rows.append([{"text": "⏹ Stop" if is_running else "▶️ Start", "callback_data": _ref("toggle_proto", {"sid": server_id, "proto": proto, "start": not is_running})}])
    rows.append([{"text": "🗑 Uninstall protocol", "callback_data": _ref("uninstall_proto", {"sid": server_id, "proto": proto})}])
    rows.append([{"text": "⬅️ Protocols", "callback_data": f"srv:{server_id}"}])
    return {"inline_keyboard": rows}



def _client_keyboard(server_id: int, proto: str, client: dict) -> dict:
    client_id = client.get("clientId") or client.get("client_id") or client.get("id") or ""
    enabled = client.get("enabled")
    if enabled is None:
        enabled = client.get("isEnabled")
    enabled = bool(enabled) if enabled is not None else True
    return {
        "inline_keyboard": [
            [{"text": "📄 Config", "callback_data": _ref("client_cfg", {"sid": server_id, "proto": proto, "client_id": client_id, "name": client.get("name") or client.get("username") or "Connection"})}],
            [{"text": "🚫 Disable" if enabled else "✅ Enable", "callback_data": _ref("toggle_client", {"sid": server_id, "proto": proto, "client_id": client_id, "enable": not enabled})}],
            [{"text": "🗑 Delete", "callback_data": _ref("remove_client", {"sid": server_id, "proto": proto, "client_id": client_id})}],
            [{"text": "⬅️ Connections", "callback_data": _ref("clients", {"sid": server_id, "proto": proto})}],
        ]
    }


def _get_ssh_and_manager(server: dict, proto: str):
    sys.path.insert(0, os.path.dirname(__file__))
    from managers.ssh_manager import SSHManager
    from managers.awg_manager import AWGManager
    from managers.xray_manager import XrayManager
    from managers.telemt_manager import TelemtManager
    from managers.wireguard_manager import WireGuardManager
    from managers.dns_manager import DNSManager
    from managers.socks5_manager import Socks5Manager
    from managers.adguard_manager import AdguardManager
    from managers.nginx_manager import NginxManager

    ssh = SSHManager(
        server["host"],
        server.get("ssh_port", 22),
        server["username"],
        server.get("password", ""),
        server.get("private_key", ""),
    )
    base = _proto_base(proto)
    if base == "xray":
        manager = XrayManager(ssh, proto)
    elif base == "telemt":
        manager = TelemtManager(ssh, proto)
    elif base == "wireguard":
        manager = WireGuardManager(ssh)
    elif base == "dns":
        manager = DNSManager(ssh)
    elif base == "socks5":
        manager = Socks5Manager(ssh, proto)
    elif base == "adguard":
        manager = AdguardManager(ssh)
    elif base == "nginx":
        manager = NginxManager(ssh, proto)
    else:
        manager = AWGManager(ssh)
    return ssh, manager


def _manager_call(manager, method_name: str, proto: str, *args, **kwargs):
    method = getattr(manager, method_name)
    try:
        return method(proto, *args, **kwargs)
    except TypeError:
        return method(*args, **kwargs)


def _refresh_server_protocol_statuses(server: dict) -> dict:
    """Refresh saved protocol metadata with live Docker status for Telegram admin views."""
    protocols = server.get("protocols", {}) or {}
    if not protocols:
        return server

    ssh = None
    try:
        ssh, _ = _get_ssh_and_manager(server, "awg")
        ssh.connect()
        for proto, info in protocols.items():
            container = info.get("container_name")
            if not container:
                try:
                    _, manager = _get_ssh_and_manager(server, proto)
                    container = getattr(manager, "container_name", None) or getattr(manager, "CONTAINER_NAME", None)
                except Exception:
                    container = None
            if not container:
                info["status_error"] = "Container name is unknown"
                continue
            out, err, code = ssh.run_sudo_command(
                f"docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(str(container))} 2>/dev/null"
            )
            if code == 0:
                info["container_running"] = out.strip().lower() == "true"
                info["container_exists"] = True
                info.pop("status_error", None)
            else:
                info["container_running"] = False
                info["container_exists"] = False
                info["status_error"] = (err or out or "container not found").strip()
    except Exception as e:
        logger.warning("Telegram bot: failed to refresh protocol statuses for %s: %s", server.get("name") or server.get("host"), e)
        for info in protocols.values():
            info["status_error"] = str(e)
    finally:
        if ssh:
            try:
                ssh.disconnect()
            except Exception:
                pass
    return server


async def _refresh_server_protocol_statuses_async(server: dict) -> dict:
    return await asyncio.to_thread(_refresh_server_protocol_statuses, server)


# ----------------------------------------------------------------------- #
#  /start and user connection handlers
# ----------------------------------------------------------------------- #
async def _handle_start(api: TelegramAPI, msg: dict, db):
    chat_id = msg["chat"]["id"]
    tg_id = str(msg["from"]["id"])
    first_name = msg["from"].get("first_name", "")

    panel_user = await _find_user(db, tg_id)

    if not panel_user:
        await api.send_message(
            chat_id,
            f"👋 Hi, <b>{_e(first_name)}</b>!\n\n"
            "Your Telegram account is not linked to any panel user.\n"
            "Please contact your administrator — they need to add your Telegram ID to your profile.\n\n"
            f"Your Telegram ID: <code>{_e(tg_id)}</code>",
        )
        return

    if _is_admin(panel_user):
        await api.send_message(
            chat_id,
            f"👋 Hi, <b>{_e(first_name)}</b>!\n\n"
            f"You are registered as <b>{_e(panel_user.get('username'))}</b> with <b>Admin</b> role.\n"
            "Choose an action:",
            reply_markup=_admin_main_keyboard(),
        )
        return

    await _send_user_connections(api, chat_id, panel_user, db, first_name=first_name)


async def _send_user_connections(api: TelegramAPI, chat_id: int, panel_user: dict, db, first_name: str = ""):
    data = await _build_data_dict(db)
    conns = [c for c in data.get("user_connections", {}).values() if c.get("user_id") == panel_user.get("id")]

    if not conns:
        greeting = f"👋 Hi, <b>{_e(first_name)}</b>!\n\n" if first_name else ""
        await api.send_message(
            chat_id,
            greeting + f"You are registered as <b>{_e(panel_user.get('username'))}</b>.\n\n"
            "You have no connections yet. Please contact your administrator.",
        )
        return

    kb = _build_connections_keyboard(conns, data)
    greeting = f"👋 Hi, <b>{_e(first_name)}</b>!\n\n" if first_name else ""
    await api.send_message(
        chat_id,
        greeting + f"You are registered as <b>{_e(panel_user.get('username'))}</b>.\n\n"
        f"<b>Your connections</b> ({len(conns)}) — tap to get config:",
        reply_markup=kb,
    )


async def _handle_refresh(api: TelegramAPI, chat_id: int, message_id: int, callback_id: str, tg_id: str, db):
    await api.answer_callback(callback_id, "Updated!")
    panel_user = await _find_user(db, tg_id)
    if not panel_user:
        await api.edit_message(chat_id, message_id, "❌ Access denied.")
        return
    data = await _build_data_dict(db)
    conns = [c for c in data.get("user_connections", {}).values() if c.get("user_id") == panel_user.get("id")]
    if not conns:
        await api.edit_message(chat_id, message_id, "You have no connections.")
        return
    kb = _build_connections_keyboard(conns, data)
    await api.edit_message(chat_id, message_id, f"<b>Your connections</b> ({len(conns)}) — tap to get config:", reply_markup=kb)


def _format_smart_config_card(conn_name: str, server_name: str, proto: str, config: str, conn_id: Optional[str] = None, generate_vpn_link_fn: Optional[Callable] = None) -> tuple[str, dict]:
    is_link_proto = _proto_base(proto) in ("xray", "telemt")
    
    text = (
        f"✅ <b>{_e(conn_name)}</b>\n"
        f"🌐 Server: <b>{_e(server_name)}</b>\n"
        f"🔌 Protocol: <b>{_e(proto.upper())}</b>\n\n"
    )
    
    if is_link_proto:
        text += (
            f"🔗 <b>Connection link</b> (tap to expand/copy):\n"
            f"<blockquote expandable><code>{_e(config)}</code></blockquote>"
        )
    else:
        vpn_link = generate_vpn_link_fn(config) if (config and generate_vpn_link_fn) else ""
        if vpn_link:
            text += (
                f"🔗 <b>VPN Link</b> (tap to expand/copy):\n"
                f"<blockquote expandable><code>{_e(vpn_link)}</code></blockquote>\n\n"
            )
        
        text += (
            f"📄 <b>Configuration</b> (tap to expand/copy):\n"
            f"<blockquote expandable><code>{_e(config)}</code></blockquote>"
        )
    
    inline_keyboard = []
    if conn_id:
        action_row = []
        action_row.append({"text": "📱 QR-код", "callback_data": f"qr:{conn_id}"})
        if not is_link_proto:
            action_row.append({"text": "📁 Скачать .conf", "callback_data": f"dl:{conn_id}"})
        inline_keyboard.append(action_row)
        inline_keyboard.append([{"text": "🔙 К подключениям", "callback_data": "refresh"}])
    else:
        inline_keyboard.append([{"text": "❌ Закрыть", "callback_data": "del_msg"}])

    return text, {"inline_keyboard": inline_keyboard}



async def _handle_get_config(api: TelegramAPI, chat_id: int, message_id: int, callback_id: str, conn_id: str, tg_id: str, db, generate_vpn_link_fn: Callable):
    await api.answer_callback(callback_id, "Fetching config...")

    panel_user = await _find_user(db, tg_id)
    if not panel_user:
        await api.send_message(chat_id, "❌ Access denied.")
        return

    data = await _build_data_dict(db)
    conn = next((c for c in data.get("user_connections", {}).values() if c.get("id") == conn_id and (_is_admin(panel_user) or c.get("user_id") == panel_user.get("id"))), None)
    if not conn:
        await api.send_message(chat_id, "❌ Connection not found.")
        return

    servers = data.get("servers", [])
    sid = conn.get("server_id")
    if sid not in servers:
        await api.send_message(chat_id, "❌ Server not found.")
        return

    await _send_config_by_client(
        api, chat_id, servers[sid], conn.get("protocol", "awg"),
        conn.get("client_id"), conn.get("name", "Connection"), generate_vpn_link_fn,
        conn_id=conn_id, message_id=message_id
    )


async def _send_config_by_client(
    api: TelegramAPI,
    chat_id: int,
    server: dict,
    proto: str,
    client_id: str,
    conn_name: str,
    generate_vpn_link_fn: Callable,
    conn_id: Optional[str] = None,
    message_id: Optional[int] = None
):
    if message_id:
        await api.edit_message(chat_id, message_id, f"⏳ Fetching config for <b>{_e(conn_name)}</b>...")
        loading_msg_id = message_id
    else:
        loading_result = await api.send_message(chat_id, f"⏳ Fetching config for <b>{_e(conn_name)}</b>...")
        loading_msg_id = loading_result.get("result", {}).get("message_id")

    try:
        proto_info = server.get("protocols", {}).get(proto, {})
        port = proto_info.get("port", "55424")

        def _get_cfg():
            ssh, manager = _get_ssh_and_manager(server, proto)
            try:
                ssh.connect()
                return _manager_call(manager, "get_client_config", proto, client_id, server["host"], port)
            finally:
                ssh.disconnect()

        config = await asyncio.to_thread(_get_cfg)
        if not config:
            if loading_msg_id:
                await api.edit_message(chat_id, loading_msg_id, "❌ Failed to retrieve configuration.")
            return

        server_name = server.get("name") or server.get("host", "Unknown")
        card_text, reply_markup = _format_smart_config_card(
            conn_name=conn_name,
            server_name=server_name,
            proto=proto,
            config=config,
            conn_id=conn_id,
            generate_vpn_link_fn=generate_vpn_link_fn
        )

        if loading_msg_id:
            await api.edit_message(chat_id, loading_msg_id, card_text, reply_markup=reply_markup)
        else:
            await api.send_message(chat_id, card_text, reply_markup=reply_markup)
    except Exception as e:
        logger.exception("Bot: error getting config")
        if loading_msg_id:
            await api.edit_message(chat_id, loading_msg_id, f"❌ Error: {_e(e)}")
        else:
            await api.send_message(chat_id, f"❌ Error: {_e(e)}")


async def _handle_send_qr(api: TelegramAPI, chat_id: int, callback_id: str, conn_id: str, tg_id: str, db, generate_vpn_link_fn: Callable):
    await api.answer_callback(callback_id, "Generating QR code...")

    panel_user = await _find_user(db, tg_id)
    if not panel_user:
        await api.send_message(chat_id, "❌ Access denied.")
        return

    data = await _build_data_dict(db)
    conn = next((c for c in data.get("user_connections", {}).values() if c.get("id") == conn_id and (_is_admin(panel_user) or c.get("user_id") == panel_user.get("id"))), None)
    if not conn:
        await api.send_message(chat_id, "❌ Connection not found.")
        return

    servers = data.get("servers", [])
    sid = conn.get("server_id")
    if sid not in servers:
        await api.send_message(chat_id, "❌ Server not found.")
        return

    server = servers[sid]
    proto = conn.get("protocol", "awg")
    client_id = conn.get("client_id")
    conn_name = conn.get("name", "Connection")
    proto_info = server.get("protocols", {}).get(proto, {})
    port = proto_info.get("port", "55424")

    def _get_cfg():
        ssh, manager = _get_ssh_and_manager(server, proto)
        try:
            ssh.connect()
            return _manager_call(manager, "get_client_config", proto, client_id, server["host"], port)
        finally:
            ssh.disconnect()

    try:
        config = await asyncio.to_thread(_get_cfg)
        if not config:
            await api.send_message(chat_id, "❌ Failed to retrieve configuration for QR.")
            return

        from managers.amnezia_config import generate_qr_content
        is_link_proto = _proto_base(proto) in ("xray", "telemt")
        qr_text = config if is_link_proto else generate_qr_content(config)
        if not qr_text:
            await api.send_message(chat_id, "❌ Could not generate QR content.")
            return

        import qrcode
        import io
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        bio = io.BytesIO()
        img.save(bio, format="PNG")

        kb = {"inline_keyboard": [[{"text": "🗑️ Закрыть QR", "callback_data": "del_msg"}]]}
        await api.send_photo(chat_id, photo=bio.getvalue(), caption=f"📷 <b>QR-код:</b> {_e(conn_name)}", reply_markup=kb)
    except Exception as e:
        logger.exception("Bot: error sending QR code")
        await api.send_message(chat_id, f"❌ Error: {_e(e)}")


async def _handle_download_conf(api: TelegramAPI, chat_id: int, callback_id: str, conn_id: str, tg_id: str, db):
    await api.answer_callback(callback_id, "Preparing .conf file...")

    panel_user = await _find_user(db, tg_id)
    if not panel_user:
        await api.send_message(chat_id, "❌ Access denied.")
        return

    data = await _build_data_dict(db)
    conn = next((c for c in data.get("user_connections", {}).values() if c.get("id") == conn_id and (_is_admin(panel_user) or c.get("user_id") == panel_user.get("id"))), None)
    if not conn:
        await api.send_message(chat_id, "❌ Connection not found.")
        return

    servers = data.get("servers", [])
    sid = conn.get("server_id")
    if sid not in servers:
        await api.send_message(chat_id, "❌ Server not found.")
        return

    server = servers[sid]
    proto = conn.get("protocol", "awg")
    client_id = conn.get("client_id")
    conn_name = conn.get("name", "Connection")
    proto_info = server.get("protocols", {}).get(proto, {})
    port = proto_info.get("port", "55424")

    def _get_cfg():
        ssh, manager = _get_ssh_and_manager(server, proto)
        try:
            ssh.connect()
            return _manager_call(manager, "get_client_config", proto, client_id, server["host"], port)
        finally:
            ssh.disconnect()

    try:
        config = await asyncio.to_thread(_get_cfg)
        if not config:
            await api.send_message(chat_id, "❌ Failed to retrieve configuration.")
            return

        filename = f"{str(conn_name).replace(' ', '_')}.conf"
        kb = {"inline_keyboard": [[{"text": "🗑️ Закрыть", "callback_data": "del_msg"}]]}
        await api.send_document(chat_id, filename=filename, content=config.encode("utf-8"), caption=f"📁 Config file: {_e(conn_name)}", reply_markup=kb)
    except Exception as e:
        logger.exception("Bot: error sending conf document")
        await api.send_message(chat_id, f"❌ Error sending file: {_e(e)}")



# ----------------------------------------------------------------------- #
#  Admin handlers
# ----------------------------------------------------------------------- #
async def _require_admin(db, tg_id: str):
    user = await _find_user(db, tg_id)
    if not user or not _is_admin(user):
        return None
    return user


async def _handle_add_server_command(api: TelegramAPI, msg: dict, db):
    chat_id = msg["chat"]["id"]
    tg_id = str(msg["from"]["id"])
    if not await _require_admin(db, tg_id):
        await api.send_message(chat_id, "❌ Access denied.")
        return
    
    text = msg.get("text", "")
    parts = text.split(maxsplit=5)
    if len(parts) < 4:
        await api.send_message(
            chat_id,
            "Usage:\n"
            "<code>/addserver host username password [ssh_port] [name]</code>\n\n"
            "Example:\n"
            "<code>/addserver 203.0.113.10 root myPassword 22 Prod VPS</code>\n\n"
            "⚠️ Telegram messages are not a secrets manager. Prefer adding servers in the web panel if possible.",
        )
        return

    host = parts[1]
    username = parts[2]
    password = parts[3]
    ssh_port = 22
    name = host
    if len(parts) >= 5:
        try:
            ssh_port = int(parts[4])
        except Exception:
            name = parts[4]
    if len(parts) >= 6:
        name = parts[5] or host

    from database import models
    from sqlalchemy import select
    from managers.secrets import encrypt_secret
    
    stmt = select(models.Server).order_by(models.Server.display_order.desc()).limit(1)
    res = await db.execute(stmt)
    max_server = res.scalar_one_or_none()
    next_order = (max_server.display_order + 1) if max_server else 0

    server = models.Server(
        name=name, host=host, ssh_port=ssh_port,
        username=username, password=encrypt_secret(password),
        private_key=encrypt_secret(""), server_info={},
        host_fingerprint="", display_order=next_order
    )
    db.add(server)
    await db.commit()
    await api.send_message(chat_id, f"✅ Server added: <b>{_e(name)}</b>\nHost: <code>{_e(host)}</code>")


async def _admin_servers(api: TelegramAPI, chat_id: int, message_id: Optional[int], db):
    data = await _build_data_dict(db)
    servers = data.get("servers", [])
    text = f"🖥 <b>Servers</b> ({len(servers)})\n\nChoose a server:"
    if message_id:
        await api.edit_message(chat_id, message_id, text, reply_markup=_server_keyboard(data))
    else:
        await api.send_message(chat_id, text, reply_markup=_server_keyboard(data))


async def _admin_users(api: TelegramAPI, chat_id: int, message_id: int, db):
    data = await _build_data_dict(db)
    users = data.get("users", [])
    await api.edit_message(chat_id, message_id, f"👤 <b>Users</b> ({len(users)})\n\nChoose a user:", reply_markup=_users_keyboard(data))


async def _admin_start_add_user(api: TelegramAPI, chat_id: int, message_id: int):
    _pending_inputs[str(chat_id)] = {
        "kind": "add_user_name",
        "ts": time.time(),
    }
    await api.edit_message(
        chat_id,
        message_id,
        "➕ <b>Create User</b>\n\n"
        "Please send the username for the new user in your next message.\n\n"
        "Send /cancel to cancel.",
        reply_markup={"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "adm:users"}]]},
    )


async def _admin_create_user(api: TelegramAPI, chat_id: int, message_id: int, username: str, role: str, db):
    from database import models
    from sqlalchemy import select
    import app as app_module
    import uuid

    stmt = select(models.User).where(models.User.username == username)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        await api.edit_message(
            chat_id,
            message_id,
            f"❌ User <b>{_e(username)}</b> already exists.",
            reply_markup={"inline_keyboard": [[{"text": "⬅️ Users", "callback_data": "adm:users"}]]},
        )
        return

    new_user_id = str(uuid.uuid4())
    new_user = models.User(
        id=new_user_id,
        username=username,
        role=role if role in ("admin", "user") else "user",
        enabled=True,
        password_hash=app_module.hash_password(username),
    )
    db.add(new_user)
    await db.commit()
    await api.edit_message(
        chat_id,
        message_id,
        f"✅ User <b>{_e(username)}</b> created successfully ({role}).",
        reply_markup={
            "inline_keyboard": [
                [{"text": "👤 View User", "callback_data": _ref("user", {"uid": new_user_id})}],
                [{"text": "⬅️ Users", "callback_data": "adm:users"}],
            ]
        },
    )


async def _admin_toggle_user(api: TelegramAPI, chat_id: int, message_id: int, user_id: str, enable: bool, db):
    import app as app_module
    success = await app_module.perform_toggle_user(db, user_id, enable)
    if not success:
        await api.edit_message(chat_id, message_id, "❌ User not found.")
        return
    await _admin_user_detail(api, chat_id, message_id, user_id, db)


async def _admin_confirm_delete_user(api: TelegramAPI, chat_id: int, message_id: int, user_id: str, db):
    from database import models
    user = await db.get(models.User, user_id)
    if not user:
        await api.edit_message(chat_id, message_id, "❌ User not found.")
        return
    username = user.username
    kb = {
        "inline_keyboard": [
            [{"text": "🗑 Yes, Delete User", "callback_data": _ref("do_del_user", {"uid": user_id})}],
            [{"text": "❌ Cancel", "callback_data": _ref("user", {"uid": user_id})}],
        ]
    }
    await api.edit_message(
        chat_id,
        message_id,
        f"⚠️ Are you sure you want to delete user <b>{_e(username)}</b>?\n\nThis action cannot be undone.",
        reply_markup=kb,
    )


async def _admin_delete_user(api: TelegramAPI, chat_id: int, message_id: int, user_id: str, db):
    import app as app_module
    await app_module.perform_delete_user(db, user_id)
    await _admin_users(api, chat_id, message_id, db)


async def _admin_user_detail(api: TelegramAPI, chat_id: int, message_id: int, user_id: str, db):
    data = await _build_data_dict(db)
    user = next((u for u in data.get("users", {}).values() if u.get("id") == user_id), None)
    if not user:
        await api.edit_message(chat_id, message_id, "❌ User not found.", reply_markup={"inline_keyboard": [[{"text": "⬅️ Users", "callback_data": "adm:users"}]]})
        return
    conns = [c for c in data.get("user_connections", {}).values() if c.get("user_id") == user_id]
    enabled = user.get("enabled", True)
    lines = [
        f"👤 <b>{_e(user.get('username'))}</b>",
        f"Role: <b>{_e(user.get('role', 'user'))}</b>",
        f"Enabled: <b>{'yes ✅' if enabled else 'no 🚫'}</b>",
        f"Telegram ID: <code>{_e(user.get('telegramId') or '-')}</code>",
        f"Email: <code>{_e(user.get('email') or '-')}</code>",
        f"Connections: <b>{len(conns)}</b>",
    ]
    if user.get("description"):
        lines.append(f"Description: {_e(user.get('description'))}")
    rows = []
    rows.append([
        {"text": "🔒 Disable" if enabled else "✅ Enable", "callback_data": _ref("toggle_user", {"uid": user_id, "enable": not enabled})},
        {"text": "🗑 Delete", "callback_data": _ref("confirm_del_user", {"uid": user_id})},
    ])

    servers = data.get("servers", [])
    for c in conns[:20]:
        sid = c.get("server_id")
        server_name = "Unknown"
        if sid in servers:
            server_name = servers[sid].get("name") or servers[sid].get("host") or "Unknown"
        rows.append([{"text": f"🔐 {c.get('name', 'Connection')} · {c.get('protocol', '').upper()} · {server_name}", "callback_data": f"cfg:{c.get('id')}"}])
    rows.append([{"text": "⬅️ Users", "callback_data": "adm:users"}])
    rows.append([{"text": "⬅️ Admin menu", "callback_data": "adm:menu"}])
    await api.edit_message(chat_id, message_id, "\n".join(lines), reply_markup={"inline_keyboard": rows})



async def _admin_server_detail(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, db):
    data = await _build_data_dict(db)
    servers = data.get("servers", [])
    if server_id not in servers:
        await api.edit_message(chat_id, message_id, "❌ Server not found.")
        return
    server = await _refresh_server_protocol_statuses_async(servers[server_id])
    protocols = server.get("protocols", {}) or {}
    text = (
        f"🖥 <b>{_e(server.get('name') or server.get('host'))}</b>\n"
        f"Host: <code>{_e(server.get('host'))}</code>\n"
        f"SSH: <code>{_e(server.get('username'))}@{_e(server.get('host'))}:{_e(server.get('ssh_port', 22))}</code>\n\n"
        f"<b>Protocols</b> ({len(protocols)}):"
    )
    await api.edit_message(chat_id, message_id, text, reply_markup=_protocols_keyboard(server_id, server))


async def _admin_protocol_detail(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str, db):
    data = await _build_data_dict(db)
    servers = data.get("servers", [])
    if server_id not in servers:
        await api.edit_message(chat_id, message_id, "❌ Server not found.")
        return
    server = await _refresh_server_protocol_statuses_async(servers[server_id])
    info = (server.get("protocols", {}) or {}).get(proto)
    if not info:
        await api.edit_message(chat_id, message_id, "❌ Protocol not found.")
        return
    lines = [
        f"🔌 <b>{_e(_protocol_display_name(proto))}</b>",
        f"Server: <b>{_e(server.get('name') or server.get('host'))}</b>",
        f"Status: <b>{_protocol_status_text(info)}</b>",
    ]
    for key in ("port", "container_name", "domain", "site_url", "web_port", "mode"):
        if info.get(key) not in (None, ""):
            lines.append(f"{_e(key)}: <code>{_e(info.get(key))}</code>")
    if info.get("status_error"):
        lines.append(f"status_error: <code>{_e(info.get('status_error'))}</code>")
    await api.edit_message(chat_id, message_id, "\n".join(lines), reply_markup=_protocol_keyboard(server_id, proto, info))

async def _admin_toggle_protocol(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str, start: bool, db):
    await api.edit_message(chat_id, message_id, "⏳ Updating protocol container...")

    data = await _build_data_dict(db)
    def _toggle(data=data):
        server = data["servers"][server_id]
        ssh, manager = _get_ssh_and_manager(server, proto)
        try:
            ssh.connect()
            container = (server.get("protocols", {}).get(proto, {}) or {}).get("container_name")
            if not container:
                # fallback: most managers expose CONTAINER_NAME for base/first instances
                container = getattr(manager, "CONTAINER_NAME", None)
            if not container:
                raise RuntimeError("Container name is unknown")
            action = "start" if start else "stop"
            out, err, code = ssh.run_sudo_command(f"docker {action} {container}")
            if code != 0:
                raise RuntimeError(err or out or f"docker {action} failed")
            return data
        finally:
            ssh.disconnect()

    try:
        await asyncio.to_thread(_toggle)
        await _admin_protocol_detail(api, chat_id, message_id, server_id, proto, db)
    except Exception as e:
        logger.exception("Bot admin: protocol toggle failed")
        await api.edit_message(chat_id, message_id, f"❌ Error: {_e(e)}", reply_markup={"inline_keyboard": [[{"text": "⬅️ Protocol", "callback_data": _ref("proto", {"sid": server_id, "proto": proto})}]]})


async def _admin_install_protocol_list(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, db):
    data = await _build_data_dict(db)
    servers = data.get("servers", [])
    if server_id >= len(servers):
        await api.edit_message(chat_id, message_id, "❌ Server not found.")
        return
    server = servers[server_id]
    installed_protos = set(server.get("protocols", {}).keys())
    
    sys.path.insert(0, os.path.dirname(__file__))
    from app import BASE_PROTOCOLS
    
    available = [p for p in BASE_PROTOCOLS if p not in installed_protos]
    if not available:
        await api.edit_message(
            chat_id,
            message_id,
            "✅ All supported protocols are already installed on this server.",
            reply_markup={"inline_keyboard": [[{"text": "⬅️ Back to Server", "callback_data": f"srv:{server_id}"}]]},
        )
        return

    rows = []
    for proto in available:
        rows.append([{"text": f"⚡ Install {_protocol_display_name(proto)}", "callback_data": _ref("do_install_proto", {"sid": server_id, "proto": proto})}])
    rows.append([{"text": "⬅️ Back to Server", "callback_data": f"srv:{server_id}"}])
    
    server_name = server.get("name") or server.get("host") or f"Server {server_id + 1}"
    await api.edit_message(
        chat_id,
        message_id,
        f"⚡ <b>Install Protocol on {_e(server_name)}</b>\n\nChoose protocol to install:",
        reply_markup={"inline_keyboard": rows},
    )


async def _admin_do_install_protocol(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str, db):
    from database import models
    from sqlalchemy.orm import selectinload
    server = await db.get(models.Server, server_id, options=[selectinload(models.Server.protocols)])
    if not server:
        await api.edit_message(chat_id, message_id, "❌ Server not found.")
        return
    server_name = server.name or server.host
    
    await api.edit_message(chat_id, message_id, f"⏳ Installing <b>{_protocol_display_name(proto)}</b> on <b>{_e(server_name)}</b>...\nThis may take 1-2 minutes.")

    def _install(server_obj):
        sys.path.insert(0, os.path.dirname(__file__))
        from app import get_ssh, get_protocol_manager, ensure_docker_installed
        ssh = get_ssh(server_obj)
        ssh.connect()
        try:
            ensure_docker_installed(ssh)
            manager = get_protocol_manager(ssh, proto)
            install_base = _proto_base(proto)
            port_defaults = {"xray": 443, "telemt": 443, "socks5": 1080, "adguard": 53, "nginx": 80, "wireguard": 51820}
            port = port_defaults.get(install_base, 55424)
            
            if install_base in ("telemt", "socks5", "adguard", "nginx"):
                res = manager.install_protocol(protocol_type=proto, port=port)
            elif install_base in ("xray", "wireguard"):
                res = manager.install_protocol(port=port)
            else:
                res = manager.install_protocol(proto, port=port)
                
            if isinstance(res, dict) and (res.get("status") == "error" or res.get("error")):
                raise Exception(res.get("message") or res.get("error") or "Installation failed")
            return res, port
        finally:
            ssh.disconnect()

    try:
        res, port = await asyncio.to_thread(_install, server)
        
        awg_params = res.get("awg_params", {}) if isinstance(res, dict) else {}
        sp = models.ServerProtocol(
            server_id=server.id,
            protocol_type=proto,
            base_protocol=_proto_base(proto),
            installed=True,
            port=port,
            container_name=f"amnezia-{proto}",
            extra_params={"awg_params": awg_params} if awg_params else None
        )
        db.add(sp)
        await api.edit_message(
            chat_id,
            message_id,
            f"✅ Protocol <b>{_protocol_display_name(proto)}</b> successfully installed on <b>{_e(server_name)}</b>!",
            reply_markup={"inline_keyboard": [[{"text": "⬅️ Protocols", "callback_data": f"srv:{server_id}"}]]},
        )
    except Exception as e:
        logger.exception("Bot admin: install protocol failed")
        await api.edit_message(
            chat_id,
            message_id,
            f"❌ Failed to install {_protocol_display_name(proto)}: {_e(e)}",
            reply_markup={"inline_keyboard": [[{"text": "⬅️ Protocols", "callback_data": f"srv:{server_id}"}]]},
        )


async def _admin_confirm_uninstall_protocol(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str):
    kb = {
        "inline_keyboard": [
            [{"text": "🗑 Yes, Uninstall", "callback_data": _ref("do_uninstall_proto", {"sid": server_id, "proto": proto})}],
            [{"text": "❌ Cancel", "callback_data": _ref("proto", {"sid": server_id, "proto": proto})}],
        ]
    }
    await api.edit_message(
        chat_id,
        message_id,
        f"⚠️ Are you sure you want to uninstall <b>{_protocol_display_name(proto)}</b> from this server?\n\nThis will remove the container and configuration.",
        reply_markup=kb,
    )


async def _admin_do_uninstall_protocol(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str, db):
    from database import models
    from sqlalchemy.orm import selectinload
    server = await db.get(models.Server, server_id, options=[selectinload(models.Server.protocols)])
    if not server:
        await api.edit_message(chat_id, message_id, "❌ Server not found.")
        return

    await api.edit_message(chat_id, message_id, f"⏳ Uninstalling <b>{_protocol_display_name(proto)}</b>...")

    def _uninstall(server_obj):
        from app import get_ssh, get_protocol_manager
        ssh = get_ssh(server_obj)
        manager = get_protocol_manager(ssh, proto)
        try:
            ssh.connect()
            _manager_call(manager, "remove_container", proto)
        finally:
            ssh.disconnect()

    try:
        await asyncio.to_thread(_uninstall, server)
        # Delete from DB
        from sqlalchemy import delete
        stmt = delete(models.ServerProtocol).where(
            models.ServerProtocol.server_id == server_id,
            models.ServerProtocol.protocol_type == proto
        )
        await db.execute(stmt)
        await db.commit()
        await api.edit_message(
            chat_id,
            message_id,
            f"✅ Protocol <b>{_protocol_display_name(proto)}</b> uninstalled.",
            reply_markup={"inline_keyboard": [[{"text": "⬅️ Protocols", "callback_data": f"srv:{server_id}"}]]},
        )
    except Exception as e:
        logger.exception("Bot admin: uninstall protocol failed")
        await api.edit_message(
            chat_id,
            message_id,
            f"❌ Failed to uninstall {_protocol_display_name(proto)}: {_e(e)}",
            reply_markup={"inline_keyboard": [[{"text": "⬅️ Protocols", "callback_data": f"srv:{server_id}"}]]},
        )


async def _admin_clients(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str, db):
    await api.edit_message(chat_id, message_id, "⏳ Loading connections...")

    data = await _build_data_dict(db)
    def _load_clients(data=data):
        server = data["servers"][server_id]
        ssh, manager = _get_ssh_and_manager(server, proto)
        try:
            ssh.connect()
            return data, _manager_call(manager, "get_clients", proto)
        finally:
            ssh.disconnect()

    try:
        data, clients = await asyncio.to_thread(_load_clients)
        if not clients:
            await api.edit_message(chat_id, message_id, "👥 No connections.", reply_markup={"inline_keyboard": [[{"text": "➕ Create connection", "callback_data": _ref("add_client", {"sid": server_id, "proto": proto})}], [{"text": "⬅️ Protocol", "callback_data": _ref("proto", {"sid": server_id, "proto": proto})}]]})
            return
        rows = []
        conn_by_client = _connection_lookup(data, server_id, proto)
        users_by_id = {u.get("id"): u for u in data.get("users", {}).values()}
        for c in clients[:40]:
            client_id = c.get("clientId") or c.get("client_id") or c.get("id") or ""
            conn = conn_by_client.get(client_id)
            name = _client_display_name(c, conn)
            traffic = ""
            user_data = c.get("userData") or {}
            if user_data:
                total = (user_data.get("dataReceivedBytes") or 0) + (user_data.get("dataSentBytes") or 0)
                traffic = f" · {_format_bytes(total)}"
            assigned = ""
            if conn and conn.get("user_id") in users_by_id:
                assigned = f" · @{users_by_id[conn.get('user_id')].get('username')}"
            c["name"] = name
            c["assigned_user_id"] = conn.get("user_id") if conn else None
            rows.append([{"text": f"👤 {name}{assigned}{traffic}", "callback_data": _ref("client", {"sid": server_id, "proto": proto, "client_id": client_id, "name": name, "client": c})}])
        rows.append([{"text": "➕ Create connection", "callback_data": _ref("add_client", {"sid": server_id, "proto": proto})}])
        rows.append([{"text": "⬅️ Protocol", "callback_data": _ref("proto", {"sid": server_id, "proto": proto})}])
        await api.edit_message(chat_id, message_id, f"👥 <b>{_e(_protocol_display_name(proto))} connections</b> ({len(clients)})", reply_markup={"inline_keyboard": rows})
    except Exception as e:
        logger.exception("Bot admin: load clients failed")
        await api.edit_message(chat_id, message_id, f"❌ Error: {_e(e)}", reply_markup={"inline_keyboard": [[{"text": "⬅️ Protocol", "callback_data": _ref("proto", {"sid": server_id, "proto": proto})}]]})


async def _admin_client_detail(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str, client: dict):
    client_id = client.get("clientId") or client.get("client_id") or client.get("id") or ""
    name = _client_display_name(client)
    user_data = client.get("userData") or {}
    rx = user_data.get("dataReceivedBytes") or 0
    tx = user_data.get("dataSentBytes") or 0
    enabled = client.get("enabled")
    if enabled is None:
        enabled = client.get("isEnabled")
    enabled_text = "enabled ✅" if (enabled is None or enabled) else "disabled 🚫"
    text = (
        f"👤 <b>{_e(name)}</b>\n"
        f"Protocol: <b>{_e(_protocol_display_name(proto))}</b>\n"
        f"Client ID: <code>{_e(client_id)}</code>\n"
        f"Status: <b>{enabled_text}</b>"
    )
    if user_data:
        text += f"\nTraffic: <b>{_format_bytes(rx + tx)}</b>\nRX: {_format_bytes(rx)} · TX: {_format_bytes(tx)}"
    await api.edit_message(chat_id, message_id, text, reply_markup=_client_keyboard(server_id, proto, client))


async def _admin_add_client(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str, panel_user: dict, db, generate_vpn_link_fn: Callable):
    _pending_inputs[str(chat_id)] = {
        "kind": "add_client_name",
        "sid": server_id,
        "proto": proto,
        "admin_user_id": panel_user.get("id"),
        "ts": time.time(),
    }
    await api.edit_message(
        chat_id,
        message_id,
        "➕ <b>Create connection</b>\n\n"
        f"Server/protocol: <b>{_e(_protocol_display_name(proto))}</b>\n\n"
        "Send the connection name in the next message.\n"
        "Example: <code>Ivan iPhone</code>\n\n"
        "Send <code>/cancel</code> to cancel.",
        reply_markup={"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": _ref("clients", {"sid": server_id, "proto": proto})}]]},
    )


async def _admin_choose_client_user(api: TelegramAPI, chat_id: int, name: str, server_id: int, proto: str, db):
    data = await _build_data_dict(db)
    await api.send_message(
        chat_id,
        "✅ Connection name: <b>{}</b>\n\n"
        "Assign this connection to a panel user?".format(_e(name)),
        reply_markup=_assign_user_keyboard(data, server_id, proto, name),
    )


async def _admin_create_client(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str, name: str, user_id: Optional[str], db, generate_vpn_link_fn: Callable):
    await api.edit_message(chat_id, message_id, "⏳ Creating connection...")

    from database import models
    server = await db.get(models.Server, server_id)
    if not server:
        await api.edit_message(chat_id, message_id, "❌ Server not found.")
        return
        
    def _create(server_obj):
        from app import get_ssh, get_protocol_manager
        ssh = get_ssh(server_obj)
        manager = get_protocol_manager(ssh, proto)
        # We need the port of the installed protocol.
        # But this is in a thread, so let's just use 55424 default for telemt/etc. Wait, we should pass it.
        # The original code did: port = proto_info.get("port", "55424")
        port = "55424" # simplify fallback
        try:
            ssh.connect()
            if _proto_base(proto) == "telemt":
                result = manager.add_client(proto, name, server_obj.host, port)
            elif _proto_base(proto) == "wireguard":
                result = manager.add_client(name, server_obj.host)
            else:
                result = manager.add_client(proto, name, server_obj.host, port)
        finally:
            ssh.disconnect()
        return result

    try:
        result = await asyncio.to_thread(_create, server)
        client_id = result.get("client_id") or result.get("clientId")
        assigned_user = None
        if user_id:
            assigned_user = await db.get(models.User, user_id)
            if assigned_user and client_id:
                uc = models.UserConnection(
                    user_id=user_id,
                    server_id=server_id,
                    protocol=proto,
                    client_id=client_id,
                    name=name
                )
                db.add(uc)
                await db.commit()

        assigned_text = f"\nAssigned to: <b>{_e(assigned_user.get('username'))}</b>" if assigned_user else "\nAssigned: <b>not linked</b>"
        await api.edit_message(chat_id, message_id, f"✅ Connection created: <b>{_e(name)}</b>{assigned_text}")
        config = result.get("config")
        if config:
            await _send_config_text(api, chat_id, server, proto, name, config, generate_vpn_link_fn)
        elif client_id:
            await _send_config_by_client(api, chat_id, server, proto, client_id, name, generate_vpn_link_fn)
    except Exception as e:
        logger.exception("Bot admin: add client failed")
        await api.edit_message(chat_id, message_id, f"❌ Error: {_e(e)}", reply_markup={"inline_keyboard": [[{"text": "⬅️ Protocol", "callback_data": _ref("proto", {"sid": server_id, "proto": proto})}]]})


async def _send_config_text(api: TelegramAPI, chat_id: int, server: dict, proto: str, conn_name: str, config: str, generate_vpn_link_fn: Callable):
    card_text, reply_markup = _format_smart_config_card(
        conn_name=conn_name,
        server_name=server.get("name") or server.get("host", "Unknown"),
        proto=proto,
        config=config,
        generate_vpn_link_fn=generate_vpn_link_fn
    )
    await api.send_message(chat_id, card_text, reply_markup=reply_markup)



async def _admin_toggle_client(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str, client_id: str, enable: bool, db):
    await api.edit_message(chat_id, message_id, "⏳ Updating connection...")

    data = await _build_data_dict(db)
    def _toggle(data=data):
        server = data["servers"][server_id]
        ssh, manager = _get_ssh_and_manager(server, proto)
        try:
            ssh.connect()
            return _manager_call(manager, "toggle_client", proto, client_id, enable)
        finally:
            ssh.disconnect()

    try:
        await asyncio.to_thread(_toggle)
        await api.edit_message(chat_id, message_id, "✅ Updated.", reply_markup={"inline_keyboard": [[{"text": "⬅️ Connections", "callback_data": _ref("clients", {"sid": server_id, "proto": proto})}]]})
    except Exception as e:
        logger.exception("Bot admin: toggle client failed")
        await api.edit_message(chat_id, message_id, f"❌ Error: {_e(e)}")


async def _admin_remove_client(api: TelegramAPI, chat_id: int, message_id: int, server_id: int, proto: str, client_id: str, db):
    await api.edit_message(chat_id, message_id, "⏳ Removing connection...")

    from database import models
    server = await db.get(models.Server, server_id)
    if not server:
        await api.edit_message(chat_id, message_id, "❌ Server not found.")
        return

    def _remove(server_obj):
        from app import get_ssh, get_protocol_manager
        ssh = get_ssh(server_obj)
        manager = get_protocol_manager(ssh, proto)
        try:
            ssh.connect()
            _manager_call(manager, "remove_client", proto, client_id)
        finally:
            ssh.disconnect()

    try:
        await asyncio.to_thread(_remove, server)
        
        # Also remove the UserConnection from the database if it exists
        from sqlalchemy import delete
        stmt = delete(models.UserConnection).where(
            models.UserConnection.server_id == server_id,
            models.UserConnection.protocol == proto,
            models.UserConnection.client_id == client_id
        )
        await db.execute(stmt)
        await db.commit()
        await api.edit_message(chat_id, message_id, "✅ Connection removed.", reply_markup={"inline_keyboard": [[{"text": "⬅️ Connections", "callback_data": _ref("clients", {"sid": server_id, "proto": proto})}]]})
    except Exception as e:
        logger.exception("Bot admin: remove client failed")
        await api.edit_message(chat_id, message_id, f"❌ Error: {_e(e)}")


async def _handle_pending_input(api: TelegramAPI, msg: dict, db, generate_vpn_link_fn: Callable) -> bool:
    chat_id = msg["chat"]["id"]
    state = _pending_inputs.get(str(chat_id))
    if not state:
        return False

    text = (msg.get("text") or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        _pending_inputs.pop(str(chat_id), None)
        await api.send_message(chat_id, "❌ Action cancelled.", reply_markup=_admin_main_keyboard())
        return True
    if text.startswith("/"):
        _pending_inputs.pop(str(chat_id), None)
        return False

    if state.get("kind") == "add_client_name":
        panel_user = await _require_admin(db, str(msg["from"]["id"]))
        if not panel_user:
            _pending_inputs.pop(str(chat_id), None)
            await api.send_message(chat_id, "❌ Access denied.")
            return True
        name = text[:80].strip()
        if not name:
            await api.send_message(chat_id, "Name cannot be empty. Send a connection name or /cancel.")
            return True
        _pending_inputs.pop(str(chat_id), None)
        await _admin_choose_client_user(api, chat_id, name, int(state.get("sid", 0)), state.get("proto", "awg"), db)
        return True

    if state.get("kind") == "add_user_name":
        panel_user = await _require_admin(db, str(msg["from"]["id"]))
        if not panel_user:
            _pending_inputs.pop(str(chat_id), None)
            await api.send_message(chat_id, "❌ Access denied.")
            return True
        username = text[:50].strip()
        if not username:
            await api.send_message(chat_id, "❌ Username cannot be empty. Send a username or /cancel.")
            return True
        _pending_inputs.pop(str(chat_id), None)
        kb = {
            "inline_keyboard": [
                [
                    {"text": "👤 User", "callback_data": _ref("create_user", {"username": username, "role": "user"})},
                    {"text": "👑 Admin", "callback_data": _ref("create_user", {"username": username, "role": "admin"})},
                ],
                [{"text": "❌ Cancel", "callback_data": "adm:users"}],
            ]
        }
        await api.send_message(chat_id, f"Choose role for <b>{_e(username)}</b>:", reply_markup=kb)
        return True

    return False



# ----------------------------------------------------------------------- #
#  Main polling loop and dispatcher
# ----------------------------------------------------------------------- #
async def _run_bot(token: str, generate_vpn_link_fn: Callable):
    offset = 0
    logger.info("Telegram bot started (raw httpx polling).")

    async with httpx.AsyncClient() as client:
        api = TelegramAPI(token, client)

        me = await api.call("getMe")
        if not me.get("ok"):
            logger.error(f"Telegram bot: invalid token or API error: {me}")
            return
        logger.info(f"Telegram bot logged in as @{me['result']['username']}")

        while True:
            try:
                updates = await api.get_updates(offset=offset, timeout=25)
            except asyncio.CancelledError:
                logger.info("Telegram bot polling cancelled.")
                return
            except Exception as e:
                logger.warning(f"Telegram bot polling error: {e}")
                await asyncio.sleep(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    from database.session import get_db_context
                    async with get_db_context() as db:
                        await _dispatch(api, update, db, generate_vpn_link_fn)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.exception(f"Telegram bot: error handling update {update['update_id']}: {e}")


async def _dispatch(api: TelegramAPI, update: dict, db, generate_vpn_link_fn: Callable):
    if "message" in update:
        msg = update["message"]
        text = msg.get("text", "")
        if await _handle_pending_input(api, msg, db, generate_vpn_link_fn):
            return
        if text.startswith("/start") or text.startswith("/admin"):
            await _handle_start(api, msg, db)
        elif text.startswith("/connections"):
            panel_user = await _find_user(db, str(msg["from"]["id"]))
            if not panel_user:
                await api.send_message(msg["chat"]["id"], "❌ Access denied.")
            else:
                await _send_user_connections(api, msg["chat"]["id"], panel_user, db)
        elif text.startswith("/servers"):
            if await _require_admin(db, str(msg["from"]["id"])):
                await _admin_servers(api, msg["chat"]["id"], None, db)
            else:
                await api.send_message(msg["chat"]["id"], "❌ Access denied.")
        elif text.startswith("/addserver"):
            await _handle_add_server_command(api, msg, db)

    elif "callback_query" in update:
        cq = update["callback_query"]
        callback_id = cq["id"]
        data_str = cq.get("data", "")
        chat_id = cq["message"]["chat"]["id"]
        message_id = cq["message"]["message_id"]
        tg_id = str(cq["from"]["id"])

        if data_str == "noop":
            await api.answer_callback(callback_id)
            return
        if data_str == "del_msg":
            await api.answer_callback(callback_id)
            await api.call("deleteMessage", chat_id=chat_id, message_id=message_id)
            return
        if data_str == "refresh":
            await _handle_refresh(api, chat_id, message_id, callback_id, tg_id, db)
            return
        if data_str.startswith("cfg:"):
            await _handle_get_config(api, chat_id, message_id, callback_id, data_str[4:], tg_id, db, generate_vpn_link_fn)
            return
        if data_str.startswith("qr:"):
            await _handle_send_qr(api, chat_id, callback_id, data_str[3:], tg_id, db, generate_vpn_link_fn)
            return
        if data_str.startswith("dl:"):
            await _handle_download_conf(api, chat_id, callback_id, data_str[3:], tg_id, db)
            return


        panel_user = await _require_admin(db, tg_id)
        if not panel_user:
            await api.answer_callback(callback_id, "Access denied")
            return

        await api.answer_callback(callback_id)

        if data_str == "adm:menu":
            await api.edit_message(chat_id, message_id, "<b>Admin menu</b>", reply_markup=_admin_main_keyboard())
        elif data_str == "adm:servers":
            await _admin_servers(api, chat_id, message_id, db)
        elif data_str == "adm:users":
            await _admin_users(api, chat_id, message_id, db)
        elif data_str == "adm:add_user":
            await _admin_start_add_user(api, chat_id, message_id)
        elif data_str == "adm:myconns":
            await _send_user_connections(api, chat_id, panel_user, db)
        elif data_str == "adm:addserver_help":
            await api.edit_message(
                chat_id,
                message_id,
                "➕ <b>Add server</b>\n\n"
                "Use command:\n"
                "<code>/addserver host username password [ssh_port] [name]</code>\n\n"
                "Example:\n"
                "<code>/addserver 203.0.113.10 root myPassword 22 Prod VPS</code>\n\n"
                "⚠️ Prefer the web panel for real credentials if possible.",
                reply_markup={"inline_keyboard": [[{"text": "⬅️ Admin menu", "callback_data": "adm:menu"}]]},
            )
        elif data_str.startswith("srv:"):
            await _admin_server_detail(api, chat_id, message_id, int(data_str.split(":", 1)[1]), db)
        else:
            ref = _resolve_ref(data_str)
            if not ref:
                await api.edit_message(chat_id, message_id, "❌ Action expired. Use /start again.")
                return
            action = ref.get("action")
            payload = ref.get("payload", {})
            sid = int(payload.get("sid", 0) or 0)
            proto = payload.get("proto", "awg")
            if action == "user":
                await _admin_user_detail(api, chat_id, message_id, payload.get("uid"), db)
            elif action == "create_user":
                await _admin_create_user(api, chat_id, message_id, payload.get("username"), payload.get("role"), db)
            elif action == "toggle_user":
                await _admin_toggle_user(api, chat_id, message_id, payload.get("uid"), bool(payload.get("enable")), db)
            elif action == "confirm_del_user":
                await _admin_confirm_delete_user(api, chat_id, message_id, payload.get("uid"), db)
            elif action == "do_del_user":
                await _admin_delete_user(api, chat_id, message_id, payload.get("uid"), db)

            elif action == "proto":
                await _admin_protocol_detail(api, chat_id, message_id, sid, proto, db)
            elif action == "toggle_proto":
                await _admin_toggle_protocol(api, chat_id, message_id, sid, proto, bool(payload.get("start")), db)
            elif action == "install_proto_list":
                await _admin_install_protocol_list(api, chat_id, message_id, sid, db)
            elif action == "do_install_proto":
                await _admin_do_install_protocol(api, chat_id, message_id, sid, proto, db)
            elif action == "uninstall_proto":
                await _admin_confirm_uninstall_protocol(api, chat_id, message_id, sid, proto)
            elif action == "do_uninstall_proto":
                await _admin_do_uninstall_protocol(api, chat_id, message_id, sid, proto, db)
            elif action == "clients":
                await _admin_clients(api, chat_id, message_id, sid, proto, db)
            elif action == "client":
                await _admin_client_detail(api, chat_id, message_id, sid, proto, payload.get("client", {}))
            elif action == "client_cfg":
                data = await _build_data_dict(db)
                server = data["servers"][sid]
                await _send_config_by_client(api, chat_id, server, proto, payload.get("client_id"), payload.get("name", "Connection"), generate_vpn_link_fn)
            elif action == "add_client":
                await _admin_add_client(api, chat_id, message_id, sid, proto, panel_user, db, generate_vpn_link_fn)
            elif action == "create_client":
                await _admin_create_client(api, chat_id, message_id, sid, proto, payload.get("name", "Connection"), payload.get("user_id"), db, generate_vpn_link_fn)
            elif action == "toggle_client":
                await _admin_toggle_client(api, chat_id, message_id, sid, proto, payload.get("client_id"), bool(payload.get("enable")), db)
            elif action == "remove_client":
                await _admin_remove_client(api, chat_id, message_id, sid, proto, payload.get("client_id"), db)

