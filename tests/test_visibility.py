"""共享空间的可见范围：默认所有登录账号可见，可以指定人不可见，或者只让指定人可见。"""

from __future__ import annotations

import sqlite3
import time

import pytest

from conftest import ADMIN, ALICE, BOB, make_client, personal_space_id, upload
from phonecloud import db, security
from phonecloud.app import create_app
from phonecloud.config import load_settings

DATA_URLS = ("/api/fs/list", "/api/fs/stream", "/api/fs/download", "/api/fs/thumb")


@pytest.fixture
def space(env):
    """管理员新建的共享空间，里面有一个文件；创建后没有做任何授权操作。"""
    folder = env.tmp / "photos"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"shared")
    env.admin = make_client(env.app, ADMIN)
    env.space_id = env.admin.post("/admin/api/spaces", json={"name": "家庭相册", "path": str(folder)}).json()["id"]
    return env


def can_see(env, credentials) -> bool:
    """账号能否看到共享空间：空间列表和实际读取文件必须一致。"""
    client = make_client(env.app, credentials)
    listed = env.space_id in [s["id"] for s in client.get("/api/spaces").json()["spaces"]]
    download = client.get("/api/fs/download", params={"space": env.space_id, "path": "a.txt"})
    if listed:
        assert download.status_code == 200 and download.content == b"shared"
    else:
        # 看不到的空间和不存在的空间一样，所有读取接口都是 404，不泄露任何内容
        for url in DATA_URLS:
            response = client.get(url, params={"space": env.space_id, "path": "a.txt"})
            assert response.status_code == 404, url
            assert b"shared" not in response.content
    return listed


def admin_view(env) -> dict:
    return next(s for s in env.admin.get("/admin/api/spaces").json()["spaces"] if s["id"] == env.space_id)


def set_visibility(env, visibility: str):
    return env.admin.post(f"/admin/api/spaces/{env.space_id}/visibility", json={"visibility": visibility})


def add_to_list(env, user_id: int, **extra):
    return env.admin.post("/admin/api/grants", json={"space_id": env.space_id, "user_id": user_id, **extra})


def test_new_shared_space_is_visible_to_every_logged_in_account(space):
    assert admin_view(space)["visibility"] == "all"
    assert can_see(space, ALICE) and can_see(space, BOB) and can_see(space, ADMIN)

    # 之后才创建的账号同样不需要单独授权
    space.core.create_user("carol", "1")
    assert can_see(space, ("carol", "1"))

    # 能看到创建的相册就可以上传；没登录的人什么都看不到
    alice = make_client(space.app, ALICE)
    shared = next(s for s in alice.get("/api/spaces").json()["spaces"] if s["id"] == space.space_id)
    assert shared["writable"] is True
    saved = upload(alice, "x.txt", b"hello", space=space.space_id)
    assert saved.status_code == 200
    assert (space.tmp / "photos" / "x.txt").read_bytes() == b"hello"
    anonymous = make_client(space.app)
    assert anonymous.get("/api/fs/download", params={"space": space.space_id, "path": "a.txt"}).status_code == 401


def test_disabled_account_cannot_see_shared_spaces(space):
    alice = make_client(space.app, ALICE)
    assert space.admin.post(f"/admin/api/users/{space.alice.id}/disable").status_code == 200
    assert alice.get("/api/fs/download", params={"space": space.space_id, "path": "a.txt"}).status_code == 401


def test_specific_accounts_can_be_hidden_from_a_shared_space(space):
    """指定人不可见。"""
    hidden = add_to_list(space, space.bob.id, visibility="all")
    assert hidden.status_code == 200
    assert not can_see(space, BOB)
    assert can_see(space, ALICE) and can_see(space, ADMIN)
    assert admin_view(space)["grants"] == [{"id": hidden.json()["id"], "user_id": space.bob.id, "username": "bob"}]

    assert add_to_list(space, space.bob.id).status_code == 409  # 已在名单里
    assert add_to_list(space, 9999).status_code == 404
    assert space.admin.post("/admin/api/grants", json={"space_id": 9999, "user_id": space.bob.id}).status_code == 404

    # 从名单移除后恢复可见
    assert space.admin.delete(f"/admin/api/grants/{hidden.json()['id']}").status_code == 200
    assert can_see(space, BOB)
    assert space.admin.delete(f"/admin/api/grants/{hidden.json()['id']}").status_code == 404


def test_shared_space_can_be_limited_to_specific_accounts(space):
    """指定人可见。"""
    assert set_visibility(space, "only").status_code == 200
    assert admin_view(space)["visibility"] == "only"
    # 还没指定任何人时谁都看不到，管理员也不例外
    assert not can_see(space, ALICE) and not can_see(space, BOB) and not can_see(space, ADMIN)

    allowed = add_to_list(space, space.alice.id, visibility="only")
    assert allowed.status_code == 200
    assert can_see(space, ALICE)
    assert not can_see(space, BOB) and not can_see(space, ADMIN)

    # 之后新建的账号默认也看不到
    space.core.create_user("carol", "1")
    assert not can_see(space, ("carol", "1"))

    assert space.admin.delete(f"/admin/api/grants/{allowed.json()['id']}").status_code == 200
    assert not can_see(space, ALICE)


def test_switching_visibility_clears_the_list_instead_of_inverting_it(space):
    """名单在两种范围下含义相反：切换时必须清空，否则"不可见的人"会变成"唯一可见的人"。"""
    assert add_to_list(space, space.bob.id).status_code == 200  # bob 不可见
    assert set_visibility(space, "all").status_code == 200  # 范围没变：名单保留
    assert [g["username"] for g in admin_view(space)["grants"]] == ["bob"]

    assert set_visibility(space, "only").status_code == 200
    assert admin_view(space)["grants"] == []
    assert not can_see(space, BOB) and not can_see(space, ALICE)

    assert add_to_list(space, space.alice.id).status_code == 200  # 只有 alice 可见
    assert can_see(space, ALICE) and not can_see(space, BOB)

    assert set_visibility(space, "all").status_code == 200
    assert admin_view(space)["grants"] == []
    assert can_see(space, ALICE) and can_see(space, BOB)


def test_stale_page_cannot_turn_hide_into_show(space):
    """管理页打开期间范围被改掉了：按旧页面发来的"设为不可见"必须被拒绝，而不是变成"设为可见"。"""
    assert set_visibility(space, "only").status_code == 200
    stale = add_to_list(space, space.bob.id, visibility="all")
    assert stale.status_code == 409
    assert admin_view(space)["grants"] == [] and not can_see(space, BOB)

    assert set_visibility(space, "all").status_code == 200
    stale = add_to_list(space, space.bob.id, visibility="only")
    assert stale.status_code == 409
    assert admin_view(space)["grants"] == [] and can_see(space, BOB)


def test_visibility_validation(space):
    assert set_visibility(space, "everyone").status_code == 400
    assert set_visibility(space, "").status_code == 400
    assert space.admin.post("/admin/api/spaces/9999/visibility", json={"visibility": "all"}).status_code == 404
    assert admin_view(space)["visibility"] == "all"


def test_personal_spaces_follow_the_same_visibility_rules(space):
    """个人空间和共享空间一样：默认所有登录账号可见，可以改成仅指定账号可见，切换时名单清空。"""
    alice = make_client(space.app, ALICE)
    bob = make_client(space.app, BOB)
    alice_space = personal_space_id(alice)
    upload(alice, "mine.txt", b"alice")

    def view():
        return next(s for s in space.admin.get("/admin/api/spaces").json()["spaces"] if s["id"] == alice_space)

    def visible_to(client) -> bool:
        listed = alice_space in [s["id"] for s in client.get("/api/spaces").json()["spaces"]]
        assert (client.get("/api/fs/list", params={"space": alice_space}).status_code == 200) is listed
        return listed

    assert view()["visibility"] == "all"
    assert visible_to(bob) and visible_to(space.admin) and visible_to(alice)

    # 指定 bob 不可见
    hidden = space.admin.post("/admin/api/grants", json={"space_id": alice_space, "user_id": space.bob.id, "visibility": "all"})
    assert hidden.status_code == 200
    assert not visible_to(bob) and visible_to(space.admin)

    # 改为仅指定账号可见：名单清空，bob 不会因此变成"可见的人"
    switched = space.admin.post(f"/admin/api/spaces/{alice_space}/visibility", json={"visibility": "only"})
    assert switched.status_code == 200
    assert view()["visibility"] == "only" and view()["grants"] == []
    assert not visible_to(bob) and not visible_to(space.admin)
    assert visible_to(alice)  # 所有者始终可见
    assert upload(alice, "more.txt", b"x").status_code == 200  # 也始终可以上传

    # 给指定的人授权
    assert space.admin.post("/admin/api/grants", json={"space_id": alice_space, "user_id": space.bob.id, "visibility": "only"}).status_code == 200
    assert visible_to(bob) and not visible_to(space.admin)
    assert upload(bob, "x.txt", b"x", space=alice_space).status_code == 403

    # 改回所有登录账号可见
    assert space.admin.post(f"/admin/api/spaces/{alice_space}/visibility", json={"visibility": "all"}).status_code == 200
    assert view()["grants"] == []
    assert visible_to(bob) and visible_to(space.admin)


def test_new_account_has_no_default_space_until_a_folder_is_created(space):
    space.core.create_user("carol", "1")
    carol = make_client(space.app, ("carol", "1"))
    own = [s for s in carol.get("/api/spaces").json()["spaces"] if s["kind"] == "personal" and s["writable"]]
    assert own == []
    created = carol.post("/api/folders", json={"name": "笔记", "kind": "personal"})
    assert created.status_code == 200
    folder_id = created.json()["id"]
    listed = next(s for s in space.admin.get("/admin/api/spaces").json()["spaces"] if s["id"] == folder_id)
    assert listed["visibility"] == "all" and listed["grants"] == []
    bob = make_client(space.app, BOB)
    names = {s["id"]: s["name"] for s in bob.get("/api/spaces").json()["spaces"]}
    assert names[folder_id] == "笔记"


# ---- 旧版本数据库升级 --------------------------------------------------


VISIBILITY_COLUMN = "    visibility    TEXT    NOT NULL DEFAULT 'only' CHECK (visibility IN ('all', 'only')),\n"


def build_old_database(tmp_path, version: int):
    """按旧版本的结构建一个库：三个账号各有个人空间，bob 的个人空间授权给了 alice；
    两个共享空间，"已授权"授权给了 alice，"未授权"没有授权给任何人。

    version 1：还没有 visibility 列（授权后才可见）。
    version 2：有 visibility 列，共享空间默认所有人可见，个人空间只能指定账号可见。
    """
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    granted_dir, open_dir = tmp_path / "granted", tmp_path / "open"
    for folder in (granted_dir, open_dir):
        folder.mkdir()
        (folder / "a.txt").write_bytes(b"x")

    assert VISIBILITY_COLUMN in db.SCHEMA
    conn = sqlite3.connect(home / "data" / "phonecloud.db")
    conn.executescript(db.SCHEMA.replace(VISIBILITY_COLUMN, "") if version == 1 else db.SCHEMA)
    now = int(time.time())
    for user_id, (name, password) in enumerate((ADMIN, ALICE, BOB), start=1):
        conn.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, security.hash_password(password), int(name == "admin"), now),
        )
        # 旧版本建个人空间时不写 visibility：版本 2 里它取列的默认值 'only'
        conn.execute(
            "INSERT INTO spaces (id, name, kind, owner_user_id, created_at) VALUES (?, ?, 'personal', ?, ?)",
            (user_id, name, user_id, now),
        )
    conn.execute("INSERT INTO spaces (id, name, kind, path, created_at) VALUES (10, '已授权', 'shared', ?, ?)", (str(granted_dir), now))
    conn.execute("INSERT INTO spaces (id, name, kind, path, created_at) VALUES (11, '未授权', 'shared', ?, ?)", (str(open_dir), now))
    if version == 2:
        conn.execute("UPDATE spaces SET visibility = 'all' WHERE id = 11")
    conn.execute("INSERT INTO space_grants (space_id, user_id, created_at) VALUES (10, 2, ?)", (now,))  # 共享空间授权给 alice
    conn.execute("INSERT INTO space_grants (space_id, user_id, created_at) VALUES (3, 2, ?)", (now,))  # bob 的个人空间授权给 alice
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()
    return home


def stored_visibility(home) -> dict[str, str]:
    conn = sqlite3.connect(home / "data" / "phonecloud.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        return dict(conn.execute("SELECT name, visibility FROM spaces"))
    finally:
        conn.close()


@pytest.mark.parametrize("version", [1, 2])
def test_database_from_previous_version_is_upgraded(tmp_path, version):
    """从未指定过账号的空间按新默认值对所有人可见；已经授权过账号的空间保持只有这些账号可见。"""
    home = build_old_database(tmp_path, version)
    app = create_app(load_settings(home))

    expected = {"admin": "all", "alice": "all", "bob": "only", "已授权": "only", "未授权": "all"}
    assert stored_visibility(home) == expected

    def visible(credentials) -> list[str]:
        client = make_client(app, credentials)
        return sorted(s["name"] for s in client.get("/api/spaces").json()["spaces"])

    assert visible(ALICE) == ["admin 的空间", "bob 的空间", "已授权", "我的空间", "未授权"]
    # 原来没被授权的账号，升级后也不会突然看到已经授权给别人的空间
    assert visible(BOB) == ["admin 的空间", "alice 的空间", "我的空间", "未授权"]
    assert visible(ADMIN) == ["alice 的空间", "我的空间", "未授权"]


@pytest.mark.parametrize("version", [1, 2])
def test_upgrade_runs_only_once(tmp_path, version):
    """升级后管理员把个人空间改成"仅指定账号可见"且不指定任何人：重启不能再把它改回所有人可见。"""
    home = build_old_database(tmp_path, version)
    app = create_app(load_settings(home))
    admin = make_client(app, ADMIN)
    assert admin.post("/admin/api/spaces/2/visibility", json={"visibility": "only"}).status_code == 200  # alice 的个人空间
    assert admin.post("/admin/api/spaces/11/visibility", json={"visibility": "only"}).status_code == 200  # "未授权"

    restarted = create_app(load_settings(home))
    modes = stored_visibility(home)
    assert modes["alice"] == "only" and modes["未授权"] == "only"
    bob = make_client(restarted, BOB)
    assert sorted(s["name"] for s in bob.get("/api/spaces").json()["spaces"]) == ["admin 的空间", "我的空间"]
