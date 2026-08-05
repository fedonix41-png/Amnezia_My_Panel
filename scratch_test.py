import asyncio
from database.session import engine, async_session
from database.models import Base
from database.repositories import SettingRepository
import app

async def main():
    async with async_session() as db:
        settings_dict = await SettingRepository.get(db, 'settings', default={})
        print(settings_dict)

asyncio.run(main())
