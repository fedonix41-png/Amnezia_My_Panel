import os
import re

def main():
    with open("telegram_bot.py", "r") as f:
        src = f.read()

    # We will replace load_data_fn() usage with direct DB calls.
    # To do this safely, we will just redefine the functions.

    new_funcs = """
from database.session import async_session
from database import models
from sqlalchemy import select
from sqlalchemy.orm import selectinload

async def _admin_toggle_user(api: TelegramAPI, chat_id: int, message_id: int, user_id: str, enable: bool, load_data_fn: Callable, save_data_fn: Optional[Callable]):
    async with async_session() as db:
        user = await db.get(models.User, user_id)
        if not user:
            await api.edit_message(chat_id, message_id, "❌ User not found.")
            return
        user.enabled = enable
        await db.commit()
    await _admin_user_detail(api, chat_id, message_id, user_id, load_data_fn)

async def _admin_delete_user(api: TelegramAPI, chat_id: int, message_id: int, user_id: str, load_data_fn: Callable, save_data_fn: Optional[Callable]):
    async with async_session() as db:
        user = await db.get(models.User, user_id)
        if user:
            await db.delete(user)
            await db.commit()
    await _admin_users(api, chat_id, message_id, load_data_fn)

async def _admin_create_user(api: TelegramAPI, chat_id: int, message_id: int, username: str, role: str, load_data_fn: Callable, save_data_fn: Optional[Callable]):
    async with async_session() as db:
        stmt = select(models.User).where(models.User.username == username)
        res = await db.execute(stmt)
        if res.scalars().first():
            await api.edit_message(chat_id, message_id, f"❌ User <b>{_e(username)}</b> already exists.", reply_markup={"inline_keyboard": [[{"text": "⬅️ Users", "callback_data": "adm:users"}]]})
            return
        new_user = models.User(username=username, role=role if role in ("admin", "user") else "user")
        db.add(new_user)
        await db.commit()
        new_user_id = new_user.id
    await api.edit_message(chat_id, message_id, f"✅ User <b>{_e(username)}</b> created successfully ({role}).", reply_markup={"inline_keyboard": [[{"text": "👤 View User", "callback_data": _ref("user", {"uid": new_user_id})}], [{"text": "⬅️ Users", "callback_data": "adm:users"}]]})

async def _handle_add_server_command(api: TelegramAPI, msg: dict, load_data_fn: Callable, save_data_fn: Optional[Callable]):
    chat_id = msg["chat"]["id"]
    tg_id = str(msg["from"]["id"])
    async with async_session() as db:
        panel_user = await _find_user(db, tg_id)
        if not _is_admin(panel_user):
            await api.send_message(chat_id, "❌ Access denied.")
            return

    text = msg.get("text", "")
    parts = text.split(maxsplit=5)
    if len(parts) < 4:
        await api.send_message(chat_id, "Usage:\\n<code>/addserver host username password [ssh_port] [name]</code>")
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

    async with async_session() as db:
        new_srv = models.Server(name=name, host=host, ssh_port=ssh_port, username=username, password=password)
        db.add(new_srv)
        await db.commit()

    await api.send_message(chat_id, f"✅ Server added: <b>{_e(name)}</b>\\nHost: <code>{_e(host)}</code>")
"""

    # We can inject this code. 
    # But replacing load_data entirely is cleaner.
    pass

if __name__ == "__main__":
    main()
