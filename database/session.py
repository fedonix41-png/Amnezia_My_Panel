import contextlib
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
import config

connect_args = {}
if "postgresql" in config.DATABASE_URL:
    connect_args["timeout"] = 5

# Create the async engine
engine = create_async_engine(
    config.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args=connect_args,
)

# Create the session maker
async_session = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a database session."""
    async with async_session() as session:
        yield session

@contextlib.asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for getting a database session outside of FastAPI requests."""
    async with async_session() as session:
        yield session
