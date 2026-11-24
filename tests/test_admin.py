from __future__ import annotations

import json
import os

import pytest

from conftest import ADMIN, ALICE, BOB, make_client, personal_space_id, upload
from phonecloud import winutil
from phonecloud.app import create_app
from phonecloud.config import ConfigError, load_settings

LOCAL = "127.0.0.1"


# ---- 首次初始化 -------------------------------------------------------


@pytest.fixture
def fresh(tmp_path):
    return create_app(load_settings(tmp_path / "home"))


def test_setup_creates_first_admin_only_from_the_machine_itself(fresh):
    lan = make_client(fresh)
    assert lan.get("/admin/api/state").json() == {"needs_setup": True, "setup_allowed": False, "port": 8765}
    refused = lan.post("/admin/api/setup", json={"username": "boss", "password": "secret1"})
    assert refused.status_code == 403

    local = make_client(fresh, host=LOCAL)
    assert local.get("/admin/api/state").json()["setup_allowed"] is True
    assert local.post("/admin/api/setup", json={"username": "boss", "password": ""}).status_code == 400
    assert local.post("/admin/api/setup", json={"username": "boss", "password": "secret1"}).status_code == 200
    assert local.get("/admin/api/state").json() == {"needs_setup": False, "setup_allowed": False, "port": 8765}

    # 已有管理员后，初始化入口关闭，即使从本机也不行
    again = local.post("/admin/api/setup", json={"username": "other", "password": "secret1"})
    assert again.status_code == 409
    admin = make_client(fresh, ("boss", "secret1"))
    assert [u["username"] for u in admin.get("/admin/api/users").json()["users"]] == ["boss"]


# ---- 权限 -------------------------------------------------------------


def test_admin_api_rejects_normal_accounts(env):
    alice = make_client(env.app, ALICE)
    calls = [
        ("GET", "/admin/api/users", None),
        ("POST", "/admin/api/users", {"username": "eve", "password": "secret1"}),
        ("POST", f"/admin/api/users/{env.bob.id}/disable", None),
        ("POST", f"/admin/api/users/{env.bob.id}/enable", None),
        ("POST", f"/admin/api/users/{env.bob.id}/password", {"password": "hacked1"}),
        ("DELETE", f"/admin/api/users/{env.bob.id}", None),
        ("GET", "/admin/api/spaces", None),
        ("POST", "/admin/api/spaces", {"name": "x", "path": str(env.tmp)}),
        ("DELETE", "/admin/api/spaces/1", None),
        ("POST", "/admin/api/spaces/1/visibility", {"visibility": "all"}),
        ("POST", "/admin/api/grants", {"space_id": 1, "user_id": env.alice.id}),
        ("DELETE", "/admin/api/grants/1", None),
        ("GET", "/admin/api/overview", None),
    ]
    for method, url, body in calls:
        response = alice.request(method, url, json=body)
        assert response.status_code == 403, (method, url)
    # bob 的密码没有被改掉
    assert make_client(env.app).post("/api/auth/login", json={"username": "bob", "password": "bob-pass"}).status_code == 200


# ---- 账号 -------------------------------------------------------------


def test_create_account_does_not_make_a_default_space(env):
    admin = make_client(env.app, ADMIN)
    created = admin.post("/admin/api/users", json={"username": "carol", "password": "carol-pass"})
    assert created.status_code == 200
    user_id = created.json()["id"]
    assert not (env.settings.accounts_dir / "carol").exists()
    with env.core.db.session() as conn:
        assert conn.execute("SELECT 1 FROM spaces WHERE owner_user_id = ?", (user_id,)).fetchone() is None

    carol = make_client(env.app, ("carol", "carol-pass"))
    # 没有自己的文件夹；别人已经建好的个人文件夹仍然只读可见
    spaces = carol.get("/api/spaces").json()["spaces"]
    assert [(s["name"], s["writable"]) for s in spaces] == [
        ("admin", False),
        ("alice", False),
        ("bob", False),
    ]
    assert upload(carol, "a.txt", b"x").status_code == 404
    made = carol.post("/api/folders", json={"name": "相册", "kind": "personal"})
    assert made.status_code == 200
    assert upload(carol, "a.txt", b"x", space=made.json()["id"]).status_code == 200
    assert (env.settings.accounts_dir / "carol" / "相册" / "a.txt").is_file()

    users = {u["username"]: u for u in admin.get("/admin/api/users").json()["users"]}
    assert users["carol"]["is_admin"] is False and users["carol"]["disabled"] is False
    assert users["admin"]["is_admin"] is True


@pytest.mark.parametrize(
    "username, password, status",
    [
        ("alice", "another-pass", 409),
        ("ALICE", "another-pass", 409),  # Windows 文件夹不区分大小写，账号名也不能只差大小写
        ("", "good-pass", 400),
        ("   ", "good-pass", 400),
        ("x" * 33, "good-pass", 400),
        ("dave", "", 400),
        ("dave", "p" * 129, 400),
        # 账号名就是 accounts 下的文件夹名，以下写法在 Windows 上不安全
        ("../evil", "good-pass", 400),
        ("..", "good-pass", 400),
        ("a\\b", "good-pass", 400),
        ("c:", "good-pass", 400),
        ("what?", "good-pass", 400),
        ("tab\tname", "good-pass", 400),
        ("zero\u200bwidth", "good-pass", 400),
        ("dot.", "good-pass", 400),  # Windows 会去掉结尾的点，等于占用了 "dot" 的文件夹
        ("con", "good-pass", 400),
        ("NUL.txt", "good-pass", 400),
        ("com1", "good-pass", 400),
    ],
)
def test_create_account_validation(env, username, password, status):
    admin = make_client(env.app, ADMIN)
    response = admin.post("/admin/api/users", json={"username": username, "password": password})
    assert response.status_code == status
    assert len(admin.get("/admin/api/users").json()["users"]) == 3
    assert sorted(p.name for p in env.settings.accounts_dir.iterdir()) == ["admin", "alice", "bob"]


@pytest.mark.parametrize(
    "username, password",
    [
        ("1", "1"),  # 自家使用：账号和密码都可以只有 1 位数字
        ("a", "0"),
        ("爸爸", "8"),
        ("has space", " "),
        ("x" * 32, "p" * 128),
        (".dot-first_", "a b"),
    ],
)
def test_account_name_and_password_have_no_length_or_composition_rules(env, username, password):
    admin = make_client(env.app, ADMIN)
    created = admin.post("/admin/api/users", json={"username": username, "password": password})
    assert created.status_code == 200 and created.json()["username"] == username

    assert make_client(env.app).post("/api/auth/login", json={"username": username, "password": password + "x"}).status_code == 401
    user = make_client(env.app, (username, password))
    made = user.post("/api/folders", json={"name": "文件", "kind": "personal"})
    assert made.status_code == 200
    saved = upload(user, "a.txt", b"mine", space=made.json()["id"])
    assert saved.status_code == 200
    # 文件落在这个账号自己建的文件夹里；别的账号往这个文件夹上传会被拒绝
    assert len(list((env.settings.accounts_dir / username).rglob("a.txt"))) == 1
    alice = make_client(env.app, ALICE)
    assert upload(alice, "b.txt", b"x", space=made.json()["id"]).status_code == 403
    assert list((env.settings.accounts_dir / username).rglob("b.txt")) == []


def test_account_names_differing_only_by_case_never_share_a_folder(env):
    """Windows 文件夹名不区分大小写，中文之外的其它文字（如带重音的字母）也一样。"""
    admin = make_client(env.app, ADMIN)
    assert admin.post("/admin/api/users", json={"username": "Émile", "password": "1"}).status_code == 200
    for clash in ("émile", "ÉMILE"):
        assert admin.post("/admin/api/users", json={"username": clash, "password": "1"}).status_code == 409
    assert admin.post("/admin/api/users", json={"username": "Emile", "password": "1"}).status_code == 200
    assert len(admin.get("/admin/api/users").json()["users"]) == 5


def test_disable_logs_the_account_out_and_enable_restores_it(env):
    admin = make_client(env.app, ADMIN)
    alice = make_client(env.app, ALICE)
    assert alice.get("/api/spaces").status_code == 200

    assert admin.post(f"/admin/api/users/{env.alice.id}/disable").status_code == 200
    assert alice.get("/api/spaces").status_code == 401  # 已有登录立即失效
    relogin = make_client(env.app).post("/api/auth/login", json={"username": "alice", "password": "alice-pass"})
    assert relogin.status_code == 403 and "禁用" in relogin.json()["detail"]

    assert admin.post(f"/admin/api/users/{env.alice.id}/enable").status_code == 200
    assert make_client(env.app, ALICE).get("/api/spaces").status_code == 200


def test_admin_cannot_disable_itself(env):
    admin = make_client(env.app, ADMIN)
    me = admin.get("/api/auth/me").json()["user"]["id"]
    assert admin.post(f"/admin/api/users/{me}/disable").status_code == 400
    assert admin.get("/admin/api/users").status_code == 200
    assert admin.post("/admin/api/users/9999/disable").status_code == 404


def test_delete_account_only_when_its_folders_are_empty(env):
    admin = make_client(env.app, ADMIN)
    alice = make_client(env.app, ALICE)
    me = admin.get("/api/auth/me").json()["user"]["id"]
    account = env.settings.accounts_dir / "alice"
    folder = account / "笔记"
    assert alice.post("/api/folders", json={"name": "笔记", "kind": "personal"}).status_code == 200
    (folder / "a.txt").write_bytes(b"keep")

    listed = {u["username"]: u for u in admin.get("/admin/api/users").json()["users"]}
    assert listed["alice"]["has_files"] is True
    assert listed["bob"]["has_files"] is False

    refused = admin.delete(f"/admin/api/users/{env.alice.id}")
    assert refused.status_code == 400 and "还有文件" in refused.json()["detail"]
    assert (folder / "a.txt").read_bytes() == b"keep"
    assert alice.get("/api/spaces").status_code == 200

    (folder / "a.txt").unlink()
    listed = {u["username"]: u for u in admin.get("/admin/api/users").json()["users"]}
    assert listed["alice"]["has_files"] is False

    shared = env.tmp / "album"
    shared.mkdir()
    space_id = admin.post("/admin/api/spaces", json={"name": "相册", "path": str(shared)}).json()["id"]
    grant_id = admin.post(
        "/admin/api/grants", json={"space_id": space_id, "user_id": env.alice.id, "visibility": "all"}
    ).json()["id"]
    thumb = env.settings.thumbs_dir / "alice-thumb.webp"
    thumb.write_bytes(b"t")
    with env.core.db.session() as conn:
        personal_id = conn.execute(
            "SELECT id FROM spaces WHERE owner_user_id = ? AND name = 'alice'", (env.alice.id,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO thumb_cache (space_id, rel_path, src_size, src_mtime_ns, thumb_file, created_at)"
            " VALUES (?, 'a.jpg', 1, 1, ?, 1)",
            (personal_id, thumb.name),
        )

    assert admin.delete(f"/admin/api/users/{me}").status_code == 400
    assert admin.delete("/admin/api/users/9999").status_code == 404
    assert admin.delete(f"/admin/api/users/{env.alice.id}").status_code == 200

    assert alice.get("/api/spaces").status_code == 401
    assert make_client(env.app).post(
        "/api/auth/login", json={"username": "alice", "password": "alice-pass"}
    ).status_code == 401
    names = [u["username"] for u in admin.get("/admin/api/users").json()["users"]]
    assert names == ["admin", "bob"]
    assert not account.exists()
    assert not thumb.exists()
    spaces = admin.get("/admin/api/spaces").json()["spaces"]
    assert [s["name"] for s in spaces if s["kind"] == "shared"] == ["相册"]
    assert all(s["owner_user_id"] != env.alice.id for s in spaces)
    with env.core.db.session() as conn:
        assert conn.execute("SELECT 1 FROM space_grants WHERE id = ?", (grant_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM sessions WHERE user_id = ?", (env.alice.id,)).fetchone() is None


def test_reset_password(env):
    admin = make_client(env.app, ADMIN)
    alice = make_client(env.app, ALICE)
    assert admin.post(f"/admin/api/users/{env.alice.id}/password", json={"password": ""}).status_code == 400
    assert admin.post(f"/admin/api/users/{env.alice.id}/password", json={"password": "1"}).status_code == 200
    assert alice.get("/api/spaces").status_code == 401  # 旧登录失效
    login = make_client(env.app)
    assert login.post("/api/auth/login", json={"username": "alice", "password": "alice-pass"}).status_code == 401
    assert login.post("/api/auth/login", json={"username": "alice", "password": "1"}).status_code == 200

    # 管理员改自己的密码：当前这次登录保留，其它登录失效
    other_session = make_client(env.app, ADMIN)
    me = admin.get("/api/auth/me").json()["user"]["id"]
    assert admin.post(f"/admin/api/users/{me}/password", json={"password": "admin-new-1"}).status_code == 200
    assert admin.get("/admin/api/users").status_code == 200
    assert other_session.get("/admin/api/users").status_code == 401


def test_command_line_admin_reset(env):
    """reset-admin 命令背后的逻辑：忘记密码或管理员被禁用时的恢复手段。"""
    old_session = make_client(env.app, ADMIN)
    with env.core.db.session() as conn:
        conn.execute("UPDATE users SET disabled = 1 WHERE username = 'admin'")

    env.core.upsert_admin("admin", "recovered-1")
    assert old_session.get("/admin/api/users").status_code == 401
    assert make_client(env.app, ("admin", "recovered-1")).get("/admin/api/users").status_code == 200

    # 指定一个不存在的账号名则新建管理员；普通账号也可以被提升
    env.core.upsert_admin("root2", "root2-pass")
    env.core.upsert_admin("alice", "alice-new-1")
    users = {u["username"]: u for u in old_session.app.state.core.list_users()}
    assert users["root2"]["is_admin"] and users["alice"]["is_admin"]


# ---- 空间与授权 -------------------------------------------------------


def test_space_lifecycle(env):
    admin = make_client(env.app, ADMIN)
    folder = env.tmp / "movies"
    folder.mkdir()
    (folder / "film.mp4").write_bytes(b"x")

    created = admin.post("/admin/api/spaces", json={"name": "电影", "path": f'"{folder}"'})  # 带引号的粘贴路径
    assert created.status_code == 200
    space_id = created.json()["id"]
    assert admin.post("/admin/api/spaces", json={"name": "电影", "path": str(folder)}).status_code == 409

    alice = make_client(env.app, ALICE)
    assert [s["name"] for s in alice.get("/api/spaces").json()["spaces"]] == ["alice", "admin", "bob", "电影"]
    assert [e["name"] for e in alice.get("/api/fs/list", params={"space": space_id}).json()["entries"]] == ["film.mp4"]

    listed = {s["name"]: s for s in admin.get("/admin/api/spaces").json()["spaces"]}
    assert listed["电影"]["kind"] == "shared" and listed["电影"]["exists"] is True
    assert os.path.samefile(listed["电影"]["path"], folder)
    assert listed["电影"]["visibility"] == "all" and listed["电影"]["grants"] == []
    assert listed["alice"]["kind"] == "personal" and listed["alice"]["owner"] == "alice"
    assert listed["alice"]["visibility"] == "all" and listed["alice"]["grants"] == []

    # 删除空间只删记录，不动电脑上的文件
    assert admin.delete(f"/admin/api/spaces/{space_id}").status_code == 200
    assert (folder / "film.mp4").exists()
    assert alice.get("/api/fs/list", params={"space": space_id}).status_code == 404
    assert [s["name"] for s in alice.get("/api/spaces").json()["spaces"]] == ["alice", "admin", "bob"]


@pytest.mark.parametrize("path", ["relative\\folder", "Z:\\definitely\\missing"])
def test_create_space_rejects_bad_paths(env, path):
    admin = make_client(env.app, ADMIN)
    assert admin.post("/admin/api/spaces", json={"name": "x", "path": path}).status_code == 400


def test_create_space_rejects_missing_folder_outside_home(env):
    """主目录之外不存在的路径多半是写错了：报错，而不是悄悄新建一个空文件夹。"""
    admin = make_client(env.app, ADMIN)
    missing = env.tmp / "typo-folder"
    assert admin.post("/admin/api/spaces", json={"name": "x", "path": str(missing)}).status_code == 400
    assert not missing.exists()


@pytest.mark.parametrize("body", [{"name": "手机相册"}, {"name": " 手机相册 ", "path": "  "}])
def test_space_path_defaults_to_a_folder_named_after_the_space_in_home(env, body):
    admin = make_client(env.app, ADMIN)
    assert os.path.samefile(admin.get("/admin/api/spaces").json()["default_root"], env.settings.home)

    created = admin.post("/admin/api/spaces", json=body)
    assert created.status_code == 200
    folder = env.settings.home / "手机相册"
    assert folder.is_dir()  # 默认位置的文件夹不存在时自动新建
    listed = {s["name"]: s for s in admin.get("/admin/api/spaces").json()["spaces"]}
    assert os.path.samefile(listed["手机相册"]["path"], folder) and listed["手机相册"]["exists"] is True

    # 之后在电脑上往这个文件夹里放文件，登录的账号就能看到
    (folder / "IMG_1.jpg").write_bytes(b"x")
    space_id = created.json()["id"]
    alice = make_client(env.app, ALICE)
    assert [e["name"] for e in alice.get("/api/fs/list", params={"space": space_id}).json()["entries"]] == ["IMG_1.jpg"]


def test_default_space_folder_is_reused_when_it_already_exists(env):
    admin = make_client(env.app, ADMIN)
    folder = env.settings.home / "老照片"
    folder.mkdir()
    (folder / "a.jpg").write_bytes(b"x")
    assert admin.post("/admin/api/spaces", json={"name": "老照片"}).status_code == 200
    assert (folder / "a.jpg").read_bytes() == b"x"


def test_explicit_missing_path_inside_home_is_created(env):
    admin = make_client(env.app, ADMIN)
    target = env.settings.home / "共享" / "电影"
    assert admin.post("/admin/api/spaces", json={"name": "电影", "path": str(target)}).status_code == 200
    assert target.is_dir()


@pytest.mark.parametrize("name", ["data", "data\\logs", "what?", "a:b", "con", "dot.", "..\\outside", "a\\..\\..\\b"])
def test_default_space_folder_rejects_names_that_are_not_safe_folders(env, name):
    admin = make_client(env.app, ADMIN)
    before = sorted(p.name for p in env.settings.home.iterdir())
    outside_before = sorted(p.name for p in env.settings.home.parent.iterdir())
    assert admin.post("/admin/api/spaces", json={"name": name}).status_code == 400
    assert sorted(p.name for p in env.settings.home.iterdir()) == before
    assert sorted(p.name for p in env.settings.home.parent.iterdir()) == outside_before
    assert all(s["kind"] == "personal" for s in admin.get("/admin/api/spaces").json()["spaces"])


def test_create_space_rejects_files_and_blank_names(env):
    admin = make_client(env.app, ADMIN)
    file_path = env.tmp / "a.txt"
    file_path.write_bytes(b"x")
    assert admin.post("/admin/api/spaces", json={"name": "x", "path": str(file_path)}).status_code == 400
    assert admin.post("/admin/api/spaces", json={"name": "  ", "path": str(env.tmp)}).status_code == 400


def test_personal_folder_record_can_be_removed_without_deleting_files(env):
    admin = make_client(env.app, ADMIN)
    alice = make_client(env.app, ALICE)
    alice_space = personal_space_id(alice)
    folder = env.settings.accounts_dir / "alice" / "alice"
    (folder / "a.txt").write_bytes(b"x")
    assert admin.post("/admin/api/grants", json={"space_id": alice_space, "user_id": env.alice.id}).status_code == 400
    assert admin.delete(f"/admin/api/spaces/{alice_space}").status_code == 200
    assert (folder / "a.txt").read_bytes() == b"x"
    assert alice.get("/api/fs/list", params={"space": alice_space}).status_code == 404


def test_overview(env):
    admin = make_client(env.app, ADMIN)
    body = admin.get("/admin/api/overview").json()
    assert body["port"] == 8765 and body["max_upload_mb"] == 1
    assert "localport=8765" in body["firewall_command"]
    assert "NetworkCategory Private" in body["private_network_command"]
    assert os.path.samefile(body["home"], env.settings.home)
    disk = body["disks"][0]
    assert disk["total"] >= disk["used"] > 0 and disk["free"] > 0
    assert disk["uses"][0].startswith("PhoneCloud 主目录")
    for url in body["urls"]:
        assert url.startswith("http://") and url.endswith(":8765")
    if body["urls"]:
        assert body["qr_svg"].startswith("<svg") and "viewBox" in body["qr_svg"]
        assert body["qr_svgs"][0] == body["qr_svg"]
        assert len(body["qr_svgs"]) == len(body["urls"])
    else:
        assert body["qr_svg"] == ""
        assert body["qr_svgs"] == []


def test_wireless_address_leads_and_loopback_is_omitted():
    ordered = winutil.order_lan_addresses(
        [("10.6.74.64", False), ("172.19.240.1", False), ("192.168.3.29", True), ("127.0.0.1", False)],
        "10.6.74.64",
    )
    assert ordered == ["192.168.3.29", "10.6.74.64", "172.19.240.1"]
    plain = winutil.order_lan_addresses([("10.0.0.8", False), ("10.0.0.2", False)], "10.0.0.2")
    assert plain == ["10.0.0.2", "10.0.0.8"]


def test_overview_makes_a_qr_for_every_address(env, monkeypatch):
    monkeypatch.setattr(winutil, "lan_addresses", lambda: ["192.168.1.10", "10.0.0.2"])
    body = make_client(env.app, ADMIN).get("/admin/api/overview").json()
    assert body["urls"] == ["http://192.168.1.10:8765", "http://10.0.0.2:8765"]
    assert len(body["qr_svgs"]) == 2
    assert body["qr_svg"] == body["qr_svgs"][0]
    assert body["qr_svgs"][0] != body["qr_svgs"][1]
    assert all(svg.startswith("<svg") and "viewBox" in svg for svg in body["qr_svgs"])


# ---- 页面与静态资源 ---------------------------------------------------


def test_pages_and_static_assets_are_served(env):
    client = make_client(env.app)
    for url in ("/", "/admin/", "/admin"):
        response = client.get(url)
        assert response.status_code == 200 and "text/html" in response.headers["content-type"], url
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY"
    for url in ("/static/css/app.css", "/static/js/common.js", "/static/js/app.js", "/static/js/admin.js"):
        assert client.get(url).status_code == 200, url
    assert client.get("/static/../config.json").status_code == 404
    assert client.get("/api/ping").json()["ok"] is True
    ping = client.get("/api/ping")
    assert ping.headers["cache-control"] == "no-store"
    assert ping.headers["access-control-allow-origin"] == "*"
    preflight = client.options("/api/ping")
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-private-network"] == "true"
    assert client.get("/phonecloud.html").status_code == 404


# ---- 配置 -------------------------------------------------------------


def test_config_file_is_created_with_defaults_and_can_be_edited(tmp_path):
    home = tmp_path / "home"
    settings = load_settings(home)
    assert (settings.port, settings.max_upload_mb, settings.host) == (8765, 5120, "0.0.0.0")
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert config["port"] == 8765

    config["port"] = 9000
    # 记事本保存的文件可能带 BOM
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8-sig")
    assert load_settings(home).port == 9000
    assert load_settings(home, port=9100).port == 9100  # 命令行参数优先
    assert load_settings(home, port=None).port == 9000


@pytest.mark.parametrize(
    "content",
    ['{"port": 0}', '{"port": "8765"}', '{"port": 70000}', '{"access_log": 1}', '{"host": ""}', "[1, 2]", "{not json"],
)
def test_invalid_config_is_reported(tmp_path, content):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(home)


# ---- 开机自启 ---------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="注册表仅 Windows 可用")
def test_autostart_registry_roundtrip(tmp_path):
    """用临时的注册表键验证读写逻辑，不碰真正的启动项。"""
    import winreg

    key_path = r"Software\PhoneCloudTest"
    options = {"key_path": key_path, "value_name": "PhoneCloudTestValue"}
    try:
        assert winutil.autostart_command(**options) is None
        assert winutil.uninstall_autostart(**options) is False
        home = tmp_path / "my home"
        command = winutil.install_autostart(home, **options)
        assert winutil.autostart_command(**options) == command
        assert f'"{home}"' in command  # 带空格的路径加了引号
        assert command.endswith("--background") and " serve " in command
        assert winutil.uninstall_autostart(**options) is True
        assert winutil.autostart_command(**options) is None
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except FileNotFoundError:
            pass
