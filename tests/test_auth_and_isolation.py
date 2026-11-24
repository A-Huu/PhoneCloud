from __future__ import annotations

from conftest import ADMIN, ALICE, BOB, make_client, personal_space_id, upload


def test_login_sets_cookie_and_returns_token(env):
    client = make_client(env.app)
    response = client.post("/api/auth/login", json={"username": "alice", "password": "alice-pass"})
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {"id": env.alice.id, "username": "alice", "is_admin": False}
    set_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie
    assert client.get("/api/auth/me").json()["user"]["username"] == "alice"

    # App 一类的客户端不带 Cookie，只用 Bearer 令牌
    bearer = make_client(env.app)
    assert bearer.get("/api/auth/me").status_code == 401
    ok = bearer.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert ok.json()["user"]["username"] == "alice"


def test_login_is_case_insensitive_on_username(env):
    client = make_client(env.app)
    response = client.post("/api/auth/login", json={"username": "ALICE", "password": "alice-pass"})
    assert response.status_code == 200


def test_wrong_password_and_unknown_user_look_the_same(env):
    client = make_client(env.app)
    wrong = client.post("/api/auth/login", json={"username": "alice", "password": "nope"})
    unknown = client.post("/api/auth/login", json={"username": "nobody", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "账号或密码错误"}


def test_repeated_failures_are_throttled(env):
    client = make_client(env.app)
    for _ in range(10):
        assert client.post("/api/auth/login", json={"username": "alice", "password": "x"}).status_code == 401
    blocked = client.post("/api/auth/login", json={"username": "alice", "password": "alice-pass"})
    assert blocked.status_code == 429
    # 限速按"来源 + 账号"计，不影响别的账号登录
    assert client.post("/api/auth/login", json={"username": "bob", "password": "bob-pass"}).status_code == 200


def test_logout_invalidates_token(env):
    client = make_client(env.app, ALICE)
    token = client.cookies["pc_token"]
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert make_client(env.app).get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_token_is_not_stored_in_plaintext(env):
    client = make_client(env.app, ALICE)
    token = client.cookies["pc_token"]
    with env.core.db.session() as conn:
        stored = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM sessions")]
        password_hash = conn.execute("SELECT password_hash FROM users WHERE username='alice'").fetchone()[0]
    assert token not in stored
    assert "alice-pass" not in password_hash and password_hash.startswith("pbkdf2_sha256$")


def test_every_data_endpoint_requires_login(env):
    client = make_client(env.app)
    for url in (
        "/api/spaces",
        "/api/fs/list?space=1",
        "/api/fs/thumb?space=1&path=a.jpg",
        "/api/fs/stream?space=1&path=a.jpg",
        "/api/fs/download?space=1&path=a.jpg",
        "/api/web/upload/info",
        "/admin/api/users",
        "/admin/api/overview",
    ):
        assert client.get(url).status_code == 401, url
    assert upload(client, "a.txt", b"x").status_code == 401


def set_visibility(env, space_id: int, visibility: str) -> None:
    admin = make_client(env.app, ADMIN)
    response = admin.post(f"/admin/api/spaces/{space_id}/visibility", json={"visibility": visibility})
    assert response.status_code == 200, response.text


def test_personal_spaces_are_visible_to_other_accounts_by_default_but_read_only(env):
    alice = make_client(env.app, ALICE)
    bob = make_client(env.app, BOB)
    alice_spaces = alice.get("/api/spaces").json()["spaces"]
    assert [(s["name"], s["kind"], s["writable"]) for s in alice_spaces] == [
        ("alice", "personal", True),
        ("admin", "personal", False),
        ("bob", "personal", False),
    ]

    alice_space = personal_space_id(alice)
    saved = upload(alice, "photo.txt", b"hello").json()
    assert bob.get("/api/fs/download", params={"space": alice_space, "path": saved["path"]}).content == b"hello"

    # 别人的个人空间只读：拒绝上传，且不会落任何文件
    rejected = upload(bob, "evil.txt", b"x", space=alice_space)
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "该空间为只读，不能上传"
    names = [e["name"] for e in alice.get("/api/fs/list", params={"space": alice_space, "path": saved["dir"]}).json()["entries"]]
    assert names == ["photo.txt"]
    assert bob.get("/api/fs/list", params={"space": 99999}).status_code == 404


def test_personal_space_limited_to_specific_accounts_is_isolated_from_everyone_else(env):
    alice = make_client(env.app, ALICE)
    bob = make_client(env.app, BOB)
    alice_space = personal_space_id(alice)
    upload(alice, "secret.txt", b"alice only")
    set_visibility(env, alice_space, "only")

    # 所有者自己不受影响
    alice_spaces = alice.get("/api/spaces").json()["spaces"]
    assert alice_spaces[0]["id"] == alice_space and alice_spaces[0]["writable"] is True
    listing = alice.get("/api/fs/list", params={"space": alice_space, "path": ""}).json()
    assert [e["name"] for e in listing["entries"]] == ["secret.txt"]
    assert upload(alice, "more.txt", b"x").status_code == 200

    # bob 用 alice 的空间 ID 直接请求：列目录、预览、下载、缩略图、上传全部当作空间不存在
    assert alice_space not in [s["id"] for s in bob.get("/api/spaces").json()["spaces"]]
    for url in ("/api/fs/list", "/api/fs/stream", "/api/fs/download", "/api/fs/thumb"):
        response = bob.get(url, params={"space": alice_space, "path": "secret.txt"})
        assert response.status_code == 404, url
        assert b"alice only" not in response.content
    assert upload(bob, "evil.txt", b"x", space=alice_space).status_code == 404
    # 管理员也没有特殊的查看权限
    assert make_client(env.app, ADMIN).get("/api/fs/list", params={"space": alice_space}).status_code == 404


def test_grant_gives_read_only_access_and_revoke_removes_it(env):
    """仅指定账号可见：给指定的人授权。"""
    admin = make_client(env.app, ADMIN)
    alice = make_client(env.app, ALICE)
    bob = make_client(env.app, BOB)
    alice_space = personal_space_id(alice)
    saved = upload(alice, "photo.txt", b"hello").json()
    set_visibility(env, alice_space, "only")
    assert bob.get("/api/fs/list", params={"space": alice_space}).status_code == 404

    grant = admin.post("/admin/api/grants", json={"space_id": alice_space, "user_id": env.bob.id, "visibility": "only"})
    assert grant.status_code == 200
    bob_spaces = {s["id"]: s for s in bob.get("/api/spaces").json()["spaces"]}
    assert bob_spaces[alice_space]["name"] == "alice"
    assert bob_spaces[alice_space]["writable"] is False
    assert bob.get("/api/fs/download", params={"space": alice_space, "path": saved["path"]}).content == b"hello"
    assert upload(bob, "evil.txt", b"x", space=alice_space).status_code == 403
    # 没被授权的账号（包括管理员）仍然看不到
    assert admin.get("/api/fs/list", params={"space": alice_space}).status_code == 404

    assert admin.delete(f"/admin/api/grants/{grant.json()['id']}").status_code == 200
    assert bob.get("/api/fs/list", params={"space": alice_space}).status_code == 404


def test_personal_space_can_be_hidden_from_specific_accounts(env):
    """所有登录账号可见，但指定的人不可见。"""
    admin = make_client(env.app, ADMIN)
    alice = make_client(env.app, ALICE)
    bob = make_client(env.app, BOB)
    alice_space = personal_space_id(alice)

    hidden = admin.post("/admin/api/grants", json={"space_id": alice_space, "user_id": env.bob.id, "visibility": "all"})
    assert hidden.status_code == 200
    assert bob.get("/api/fs/list", params={"space": alice_space}).status_code == 404
    assert admin.get("/api/fs/list", params={"space": alice_space}).status_code == 200
    assert alice.get("/api/fs/list", params={"space": alice_space}).status_code == 200

    # 所有者始终可见，不能把所有者自己设为不可见
    owner = admin.post("/admin/api/grants", json={"space_id": alice_space, "user_id": env.alice.id})
    assert owner.status_code == 400


def test_upload_to_someone_elses_space_is_rejected(env):
    alice = make_client(env.app, ALICE)
    bob_space = personal_space_id(make_client(env.app, BOB))
    assert upload(alice, "a.txt", b"x", space=bob_space).status_code == 403  # 看得到但只读
    set_visibility(env, bob_space, "only")
    assert upload(alice, "a.txt", b"x", space=bob_space).status_code == 404  # 看不到的空间等同于不存在
    assert upload(alice, "a.txt", b"x", space=99999).status_code == 404
    assert list((env.settings.accounts_dir / "bob").rglob("a.txt")) == []


def test_cross_site_write_requests_are_rejected(env):
    client = make_client(env.app, ALICE)
    evil = client.post("/api/auth/logout", headers={"Origin": "http://evil.example"})
    assert evil.status_code == 403
    assert client.get("/api/auth/me").status_code == 200  # 登录态没有被跨站请求注销
    same = client.post("/api/auth/logout", headers={"Origin": "http://testserver"})
    assert same.status_code == 200


def test_no_write_endpoints_for_files(env):
    """浏览接口仍然不能直接改文件。删除走单独的回收站接口。"""
    alice = make_client(env.app, ALICE)
    space = personal_space_id(alice)
    saved = upload(alice, "keep.txt", b"data").json()
    for method in ("DELETE", "PUT", "PATCH", "POST"):
        for url in ("/api/fs/list", "/api/fs/stream", "/api/fs/download", "/api/fs/thumb"):
            response = alice.request(method, url, params={"space": space, "path": saved["path"]})
            assert response.status_code == 405, (method, url)
    assert alice.get("/api/fs/download", params={"space": space, "path": saved["path"]}).content == b"data"
