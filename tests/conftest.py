import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main  # noqa: E402
from app.db import connect, init_db  # noqa: E402


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    return c


@pytest.fixture
def client(conn, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    main.set_db(conn)
    return TestClient(main.app)
