from typing import Any, Dict, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database.models import Setting

async def get_setting(db: AsyncSession, key: str, default: Any = None) -> Any:
    stmt = select(Setting).where(Setting.key == key)
    result = await db.execute(stmt)
    setting = result.scalar_one_or_none()
    return setting.value if setting else default

async def set_setting(db: AsyncSession, key: str, value: Any):
    stmt = select(Setting).where(Setting.key == key)
    result = await db.execute(stmt)
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = value
    else:
        setting = Setting(key=key, value=value)
        db.add(setting)
    await db.commit()
