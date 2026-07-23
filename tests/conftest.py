import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("AWP_DEV", "1")

@pytest.fixture(scope="session", autouse=True)
def set_env(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

import pytest_asyncio

@pytest_asyncio.fixture(autouse=True)
async def db_setup():
    from database.session import engine
    from database.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest.fixture
def app_client(db_setup):
    import app as _app
    from fastapi.testclient import TestClient
    with TestClient(_app.app, raise_server_exceptions=False) as c:
        yield c

