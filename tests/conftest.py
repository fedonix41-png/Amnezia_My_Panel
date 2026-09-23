import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("AWP_DEV", "1")
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///file:testmemdb?mode=memory&cache=shared&uri=true"

@pytest.fixture(scope="session", autouse=True)
def set_env():
    pass

import pytest_asyncio

@pytest_asyncio.fixture(autouse=True)
async def db_setup():
    from database.session import engine
    from database.models import Base
    from database.repositories import UserRepository
    from database.session import async_session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as session:
        await UserRepository.ensure_default_admin(session)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest.fixture
def app_client(db_setup):
    import app as _app
    from fastapi.testclient import TestClient

    # The database fixture already performs the startup work required by
    # request tests.  Do not enter TestClient as a context manager here:
    # that would run the production lifespan for every test, including
    # background traffic/backup jobs and host tunnel shutdown.
    client = TestClient(_app.app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()

