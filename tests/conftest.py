from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from phonecloud import security
from phonecloud.app import create_app
from phonecloud.config import load_settings

ADMIN = ("admin", "admin-pass")
ALICE = ("alice", "alice-pass")
BOB = ("bob", "bob-pass")


@pytest.fixture(autouse=True)
def fast_password_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    # 正式的迭代次数故意很慢，测试里调低
    monkeypatch.setattr(security, "PBKDF2_ITERATIONS", 1000)


def make_client(app, credentials: tuple[str, str] | None = None, *, host: str = "192.168.1.50") -> TestClient:
    client = TestClient(app, client=(host, 50000))
    if credentials:
        response = client.post("/api/auth/login", json={"username": credentials[0], "password": credentials[1]})
        assert response.status_code == 200, response.text
    return client


@pytest.fixture
def env(tmp_path: Path) -> SimpleNamespace:
    """一个带管理员和 alice、bob 两个普通账号的全新实例。上传上限设为 1 MB 方便测超限。"""
    settings = load_settings(tmp_path / "home", max_upload_mb=1, min_free_mb=0)
    app = create_app(settings)
    core = app.state.core
    admin = core.create_user(*ADMIN, is_admin=True)
    alice = core.create_user(*ALICE)
    bob = core.create_user(*BOB)
    for user in (admin, alice, bob):
        core.create_folder(user, user.username, "personal")
    return SimpleNamespace(app=app, core=core, settings=settings, tmp=tmp_path, alice=alice, bob=bob)


def personal_space_id(client: TestClient) -> int:
    spaces = client.get("/api/spaces").json()["spaces"]
    return next(s["id"] for s in spaces if s["kind"] == "personal" and s["writable"])


def upload(client: TestClient, name: str, content: bytes, **params: object):
    if "space" not in params:
        listed = client.get("/api/spaces")
        if listed.status_code == 200:
            own = [item["id"] for item in listed.json()["spaces"] if item["kind"] == "personal" and item["writable"]]
            if own:
                params["space"] = own[0]
    return client.post("/api/web/upload", params=params, files={"file": (name, content, "application/octet-stream")})
