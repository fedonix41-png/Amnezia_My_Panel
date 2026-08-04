import asyncio
import os
from database.session import get_db_context
from database.models import Server
from sqlalchemy import select
from managers.secrets import decrypt_secret

async def main():
    async with get_db_context() as db:
        result = await db.execute(select(Server))
        servers = result.scalars().all()
        for s in servers:
            print(f"Server {s.id} ({s.name}):")
            print(f"  password: {repr(s.password)}")
            try:
                decrypted = decrypt_secret(s.password)
                print(f"  decrypted password: {repr(decrypted)}")
            except Exception as e:
                print(f"  decrypted password: ERROR ({e})")
            
            print(f"  private_key: {repr(s.private_key)}")
            try:
                decrypted_pk = decrypt_secret(s.private_key)
                print(f"  decrypted private_key: {repr(decrypted_pk)}")
            except Exception as e:
                print(f"  decrypted private_key: ERROR ({e})")

asyncio.run(main())
