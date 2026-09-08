"""Database repository layer providing clean data access methods for models."""

import secrets
import hashlib
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import User, Server, ServerProtocol, UserConnection, ApiToken, Setting
from managers.secrets import encrypt_secret, decrypt_secret

def utcnow():
    return datetime.now(timezone.utc)

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 600000
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"{iterations}${salt}${h.hex()}"


class UserRepository:
    @staticmethod
    async def get_by_id(db: AsyncSession, user_id: str) -> Optional[User]:
        stmt = select(User).where(User.id == user_id).options(selectinload(User.connections))
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_by_username(db: AsyncSession, username: str) -> Optional[User]:
        stmt = select(User).where(User.username == username)
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_by_telegram_id(db: AsyncSession, telegram_id: int) -> Optional[User]:
        stmt = select(User).where(User.telegramId == telegram_id)
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_by_share_token(db: AsyncSession, token: str) -> Optional[User]:
        stmt = select(User).where(User.share_token == token)
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_all(db: AsyncSession) -> List[User]:
        stmt = select(User).order_by(User.created_at.desc())
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def create(db: AsyncSession, user_data: Dict[str, Any]) -> User:
        if "password_hash" not in user_data and "password" in user_data:
            user_data["password_hash"] = hash_password(user_data.pop("password"))
        user = User(**user_data)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    async def update(db: AsyncSession, user_id: str, updates: Dict[str, Any]) -> Optional[User]:
        user = await UserRepository.get_by_id(db, user_id)
        if not user:
            return None
        if "password" in updates:
            updates["password_hash"] = hash_password(updates.pop("password"))
        for k, v in updates.items():
            if hasattr(user, k):
                setattr(user, k, v)
        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    async def delete(db: AsyncSession, user_id: str) -> bool:
        stmt = delete(User).where(User.id == user_id)
        res = await db.execute(stmt)
        await db.commit()
        return res.rowcount > 0

    @staticmethod
    async def ensure_default_admin(db: AsyncSession) -> User:
        stmt = select(User)
        res = await db.execute(stmt)
        users = res.scalars().all()
        if not users:
            admin = User(
                username="admin",
                password_hash=hash_password("admin"),
                role="admin",
                enabled=True,
                share_enabled=False
            )
            db.add(admin)
            await db.commit()
            await db.refresh(admin)
            return admin
        return users[0]


class ServerRepository:
    @staticmethod
    async def get_by_id(db: AsyncSession, server_id: int) -> Optional[Server]:
        stmt = select(Server).where(Server.id == server_id).options(selectinload(Server.protocols))
        res = await db.execute(stmt)
        server = res.scalar_one_or_none()
        if server:
            pass
        return server

    @staticmethod
    async def get_all(db: AsyncSession) -> List[Server]:
        stmt = select(Server).order_by(Server.display_order.asc(), Server.id.asc()).options(selectinload(Server.protocols))
        res = await db.execute(stmt)
        servers = list(res.scalars().all())
        for s in servers:
            pass
        return servers

    @staticmethod
    async def create(db: AsyncSession, server_data: Dict[str, Any]) -> Server:
        # Encrypt sensitive fields
        if "password" in server_data and server_data["password"]:
            server_data["password"] = encrypt_secret(server_data["password"])
        if "private_key" in server_data and server_data["private_key"]:
            server_data["private_key"] = encrypt_secret(server_data["private_key"])
        
        protocols_data = server_data.pop("protocols", [])
        server = Server(**server_data)
        db.add(server)
        await db.commit()
        await db.refresh(server)

        for p_data in protocols_data:
            proto = ServerProtocol(server_id=server.id, **p_data)
            db.add(proto)
        if protocols_data:
            await db.commit()
            await db.refresh(server)

        
        return server

    @staticmethod
    async def update(db: AsyncSession, server_id: int, updates: Dict[str, Any]) -> Optional[Server]:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return None
        if "password" in updates and updates["password"]:
            updates["password"] = encrypt_secret(updates["password"])
        if "private_key" in updates and updates["private_key"]:
            updates["private_key"] = encrypt_secret(updates["private_key"])

        for k, v in updates.items():
            if hasattr(server, k):
                setattr(server, k, v)
        await db.commit()
        await db.refresh(server)
        
        return server

    @staticmethod
    async def delete(db: AsyncSession, server_id: int) -> bool:
        stmt = delete(Server).where(Server.id == server_id)
        res = await db.execute(stmt)
        await db.commit()
        return res.rowcount > 0

    @staticmethod
    def _decrypt_secrets(server: Server):
        if server.password:
            try:
                server.password = decrypt_secret(server.password)
            except Exception:
                pass
        if server.private_key:
            try:
                server.private_key = decrypt_secret(server.private_key)
            except Exception:
                pass


class ConnectionRepository:
    @staticmethod
    async def get_by_id(db: AsyncSession, conn_id: str) -> Optional[UserConnection]:
        stmt = select(UserConnection).where(UserConnection.id == conn_id)
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_by_user_id(db: AsyncSession, user_id: str) -> List[UserConnection]:
        stmt = select(UserConnection).where(UserConnection.user_id == user_id).order_by(UserConnection.created_at.desc())
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_by_server_id(db: AsyncSession, server_id: int) -> List[UserConnection]:
        stmt = select(UserConnection).where(UserConnection.server_id == server_id)
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_all(db: AsyncSession) -> List[UserConnection]:
        stmt = select(UserConnection).order_by(UserConnection.created_at.desc())
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def create(db: AsyncSession, conn_data: Dict[str, Any]) -> UserConnection:
        conn = UserConnection(**conn_data)
        db.add(conn)
        await db.commit()
        await db.refresh(conn)
        return conn

    @staticmethod
    async def delete(db: AsyncSession, conn_id: str) -> bool:
        stmt = delete(UserConnection).where(UserConnection.id == conn_id)
        res = await db.execute(stmt)
        await db.commit()
        return res.rowcount > 0


class ApiTokenRepository:
    @staticmethod
    async def get_by_hash(db: AsyncSession, token_hash: str) -> Optional[ApiToken]:
        stmt = select(ApiToken).where(ApiToken.token_hash == token_hash).options(selectinload(ApiToken.user))
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_by_user_id(db: AsyncSession, user_id: str) -> List[ApiToken]:
        stmt = select(ApiToken).where(ApiToken.user_id == user_id).order_by(ApiToken.created_at.desc())
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def create(db: AsyncSession, token_data: Dict[str, Any]) -> ApiToken:
        token = ApiToken(**token_data)
        db.add(token)
        await db.commit()
        await db.refresh(token)
        return token

    @staticmethod
    async def touch(db: AsyncSession, token_id: str) -> None:
        stmt = update(ApiToken).where(ApiToken.id == token_id).values(last_used_at=utcnow())
        await db.execute(stmt)
        await db.commit()

    @staticmethod
    async def delete(db: AsyncSession, token_id: str) -> bool:
        stmt = delete(ApiToken).where(ApiToken.id == token_id)
        res = await db.execute(stmt)
        await db.commit()
        return res.rowcount > 0


class SettingRepository:
    @staticmethod
    async def get(db: AsyncSession, key: str, default: Any = None) -> Any:
        stmt = select(Setting).where(Setting.key == key)
        res = await db.execute(stmt)
        setting = res.scalar_one_or_none()
        return setting.value if setting else default

    @staticmethod
    async def set(db: AsyncSession, key: str, value: Any) -> None:
        stmt = select(Setting).where(Setting.key == key)
        res = await db.execute(stmt)
        setting = res.scalar_one_or_none()
        if setting:
            setting.value = value
        else:
            setting = Setting(key=key, value=value)
            db.add(setting)
        await db.commit()

    @staticmethod
    async def get_all_settings(db: AsyncSession) -> Dict[str, Any]:
        stmt = select(Setting)
        res = await db.execute(stmt)
        settings = res.scalars().all()
        return {s.key: s.value for s in settings}
