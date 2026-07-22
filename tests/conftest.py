import os
import sys
import json
import tempfile

import pytest

# Put the project root on sys.path so test modules can import managers.* and app.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must be set before the app module is imported — triggers ephemeral-key
# synthesis in config.py so tests don't need real secrets.
os.environ.setdefault("AWP_DEV", "1")


@pytest.fixture
def data_file(tmp_path):
    """Create an isolated data.json for each test and yield its path.

    ``app_client`` patches ``app.DATA_FILE`` to point at this path directly
    (app.py binds DATA_FILE once at import time from the env var, so the
    module global — not the env var — is what each request actually reads).
    """
    path = tmp_path / "data.json"
    data = {
        "servers": [],
        "users": [],
        "user_connections": [],
        "api_tokens": [],
        "settings": {},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    yield str(path)


@pytest.fixture
def app_client(data_file):
    """FastAPI TestClient wired to the isolated data file."""
    from fastapi.testclient import TestClient

    import app as _app  # noqa: F811  — triggers lifespan (startup)

    # app.py binds DATA_FILE once at import time (from the env var). Since the
    # module is cached for the whole session, point the module global at each
    # test's isolated data file so tests don't share state.
    _app.DATA_FILE = data_file

    with TestClient(_app.app, raise_server_exceptions=False) as c:
        yield c
