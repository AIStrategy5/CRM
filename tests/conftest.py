import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.security import create_user  # noqa: E402

PASSWORD = "test-password-1"
ROLES = ("admin", "lead", "msl", "viewer")


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    for role in ROLES:
        create_user(c, role, PASSWORD, role)
    return c


@pytest.fixture
def anon(conn, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    main.set_db(conn)
    main._failed.clear()
    return TestClient(main.app)


@pytest.fixture
def login(anon):
    """login(role) -> TestClient authenticated as that role (with a default change reason)."""
    from fastapi.testclient import TestClient

    def _login(role="admin", reason="test change"):
        r = anon.post("/api/auth/login", json={"username": role, "password": PASSWORD})
        assert r.status_code == 200, r.text
        headers = {"Authorization": f"Bearer {r.json()['token']}"}
        if reason:
            headers["X-Change-Reason"] = reason
        return TestClient(main.app, headers=headers)
    return _login


@pytest.fixture
def client(login):
    return login("admin")
