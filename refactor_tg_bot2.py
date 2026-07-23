import os

with open("telegram_bot.py", "r") as f:
    content = f.read()

# Make it use async_session everywhere.
# Replace _find_user to use DB
content = content.replace("""def _find_user(load_data_fn: Callable, tg_id: str):
    data = load_data_fn()
    tg_id_clean = str(tg_id).lstrip("@")
    for u in data.get("users", []):
        stored = str(u.get("telegramId", "") or "").lstrip("@")
        if stored and stored == tg_id_clean:
            return u
    return None""", """async def _find_user(db, tg_id: str):
    tg_id_clean = str(tg_id).lstrip("@")
    if not tg_id_clean.isdigit(): return None
    from sqlalchemy import select
    from database import models
    stmt = select(models.User).where(models.User.telegramId == int(tg_id_clean))
    res = await db.execute(stmt)
    return res.scalars().first()""")

# Replace _is_admin
content = content.replace("""def _is_admin(panel_user: dict) -> bool:
    return str((panel_user or {}).get("role", "")).lower() == "admin\"""", """def _is_admin(panel_user) -> bool:
    return panel_user is not None and str(getattr(panel_user, "role", "")).lower() == "admin\"""")

with open("telegram_bot_2.py", "w") as f:
    f.write(content)
print("Done")
