from __future__ import annotations

import os
import shutil
import threading
import time
from collections import namedtuple
from pathlib import Path

from conftest import ADMIN, ALICE, BOB, make_client, personal_space_id, upload


def _upload_dir(env, username: str = "alice") -> Path:
    return env.settings.accounts_dir / username / username


def _leftovers(env) -> list[Path]:
    return list(env.settings.accounts_dir.rglob(".upload-*"))


def test_upload_lands_in_the_personal_folder(env):
    alice = make_client(env.app, ALICE)
    content = os.urandom(300_000)
    response = upload(alice, "IMG_001.jpg", content)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "IMG_001.jpg",
        "original_name": "IMG_001.jpg",
        "renamed": False,
        "size": 300_000,
        "space": personal_space_id(alice),
        "dir": "",
        "path": "IMG_001.jpg",
    }
    # 电脑上是明文的普通文件，资源管理器里直接可见
    assert (_upload_dir(env) / "IMG_001.jpg").read_bytes() == content
    assert _leftovers(env) == []
    # bob 的文件夹不受影响
    assert not (_upload_dir(env, "bob") / "IMG_001.jpg").exists()


def test_same_name_is_never_overwritten(env):
    alice = make_client(env.app, ALICE)
    results = [upload(alice, "IMG_001.jpg", f"version {i}".encode()).json() for i in range(3)]
    assert [r["name"] for r in results] == ["IMG_001.jpg", "IMG_001_1.jpg", "IMG_001_2.jpg"]
    assert [r["renamed"] for r in results] == [False, True, True]
    for i, name in enumerate(["IMG_001.jpg", "IMG_001_1.jpg", "IMG_001_2.jpg"]):
        assert (_upload_dir(env) / name).read_bytes() == f"version {i}".encode()


def test_concurrent_uploads_of_the_same_name_all_survive(env):
    clients = [make_client(env.app, ALICE) for _ in range(6)]
    errors: list[object] = []

    def work(index: int) -> None:
        response = upload(clients[index], "same.bin", bytes([index]) * 5000)
        if response.status_code != 200:
            errors.append(response.text)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    files = sorted(p.name for p in _upload_dir(env).iterdir())
    assert files == ["same.bin"] + [f"same_{i}.bin" for i in range(1, 6)]
    assert {p.read_bytes()[0] for p in _upload_dir(env).iterdir()} == set(range(6))


def test_client_supplied_path_cannot_escape_the_upload_folder(env):
    alice = make_client(env.app, ALICE)
    for raw, saved in (("../../../evil.txt", "evil.txt"), ("C:\\Windows\\x.dll", "x.dll"), ("a:b.txt", "a_b.txt")):
        response = upload(alice, raw, b"x")
        assert response.status_code == 200 and response.json()["name"] == saved
        assert (_upload_dir(env) / saved).is_file()
    assert not (env.settings.accounts_dir / "evil.txt").exists()
    assert not (env.settings.home / "evil.txt").exists()


def test_unicode_filename_and_mtime_are_preserved(env):
    alice = make_client(env.app, ALICE)
    taken = 1_700_000_000_000  # 2023-11-14，毫秒
    response = upload(alice, "全家福 (1).jpg", b"data", mtime=taken)
    assert response.json()["name"] == "全家福 (1).jpg"
    assert int((_upload_dir(env) / "全家福 (1).jpg").stat().st_mtime) == taken // 1000
    # 明显不合理的时间戳（未来很久）被忽略，不影响上传
    future = upload(alice, "future.jpg", b"data", mtime=99_999_999_999_999)
    assert future.status_code == 200
    assert abs((_upload_dir(env) / "future.jpg").stat().st_mtime - time.time()) < 60


def test_empty_file_can_be_uploaded(env):
    alice = make_client(env.app, ALICE)
    assert upload(alice, "empty.txt", b"").json()["size"] == 0
    assert (_upload_dir(env) / "empty.txt").read_bytes() == b""


def test_file_over_the_limit_is_rejected_without_leftovers(env):
    alice = make_client(env.app, ALICE)
    limit = env.settings.max_upload_bytes  # 测试环境为 1 MB
    assert upload(alice, "ok.bin", b"a" * limit).status_code == 200

    # 略超上限：Content-Length 预检放行（有 multipart 余量），接收过程中按实际字节数拦下
    slightly = upload(alice, "big.bin", b"a" * (limit + 1))
    assert slightly.status_code == 413
    # 远超上限：读正文之前就拒绝
    way_over = upload(alice, "huge.bin", b"a" * (limit * 3))
    assert way_over.status_code == 413
    assert "1 MB" in way_over.json()["detail"]

    assert sorted(p.name for p in _upload_dir(env).iterdir()) == ["ok.bin"]
    assert _leftovers(env) == []


def test_upload_is_refused_when_disk_is_low(env, monkeypatch):
    alice = make_client(env.app, ALICE)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10**12, 10**12 - 1000, 1000))
    response = upload(alice, "a.bin", b"x" * 5000)
    assert response.status_code == 507
    assert "磁盘剩余空间不足" in response.json()["detail"]
    assert not (_upload_dir(env) / "a.bin").exists() and _leftovers(env) == []
    assert alice.get("/api/web/upload/info", params={"space": personal_space_id(alice)}).json()["free_bytes"] == 1000


def test_reserve_space_is_kept_free(tmp_path, monkeypatch):
    """min_free_mb：上传后剩余空间不能低于保留值。"""
    from phonecloud.app import create_app
    from phonecloud.config import load_settings

    settings = load_settings(tmp_path / "home", min_free_mb=100)
    app = create_app(settings)
    user = app.state.core.create_user(*ALICE)
    app.state.core.create_folder(user, "alice", "personal")
    alice = make_client(app, ALICE)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10**12, 0, 100 * 1024 * 1024 + 2000))
    assert upload(alice, "a.bin", b"x" * 5000).status_code == 507
    assert upload(alice, "b.bin", b"x" * 100).status_code == 200
    assert alice.get("/api/web/upload/info", params={"space": personal_space_id(alice)}).json()["free_bytes"] == 2000


def test_malformed_uploads(env):
    alice = make_client(env.app, ALICE)
    space = personal_space_id(alice)
    not_multipart = alice.post("/api/web/upload", content=b"raw", headers={"Content-Type": "application/octet-stream"})
    assert not_multipart.status_code == 400
    no_file = alice.post("/api/web/upload", params={"space": space}, files={"note": (None, "just a field")})
    assert no_file.status_code == 400 and no_file.json()["detail"] == "请求中没有文件"
    two = alice.post(
        "/api/web/upload",
        params={"space": space},
        files=[("file", ("a.txt", b"1")), ("file", ("b.txt", b"2"))],
    )
    assert two.status_code == 400 and two.json()["detail"] == "一次只能上传一个文件"

    boundary = "XBOUNDARY"
    truncated = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="cut.bin"\r\n\r\n'.encode() + b"partial"
    )
    cut = alice.post(
        "/api/web/upload",
        params={"space": space},
        content=truncated,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert cut.status_code == 400 and cut.json()["detail"] == "上传数据不完整，请重新上传"
    # 所有失败的请求都没有留下文件或半截临时文件
    assert list(_upload_dir(env).iterdir()) == []


def test_extra_form_fields_are_ignored(env):
    alice = make_client(env.app, ALICE)
    response = alice.post(
        "/api/web/upload",
        params={"space": personal_space_id(alice)},
        data={"note": "hello"},
        files={"file": ("a.txt", b"payload")},
    )
    assert response.status_code == 200
    assert (_upload_dir(env) / "a.txt").read_bytes() == b"payload"


def test_upload_info_and_listing_after_upload(env):
    alice = make_client(env.app, ALICE)
    info = alice.get("/api/web/upload/info", params={"space": personal_space_id(alice)}).json()
    assert info["space"] == personal_space_id(alice)
    assert info["dir"] == ""
    assert info["max_bytes"] == 1024 * 1024 and info["free_bytes"] > 0

    upload(alice, "pic.jpg", b"x")
    listing = alice.get("/api/fs/list", params={"space": info["space"], "path": info["dir"]}).json()
    assert [e["name"] for e in listing["entries"]] == ["pic.jpg"]
    assert listing["space"]["writable"] is True


def test_stale_temp_files_are_cleaned_on_startup(env):
    alice = make_client(env.app, ALICE)
    upload(alice, "done.jpg", b"x")
    stale = _upload_dir(env) / ".upload-deadbeef.part"
    stale.write_bytes(b"half")
    keep = _upload_dir(env) / "notes.part"
    keep.write_bytes(b"user file")
    assert env.core.cleanup_stale_uploads() == 1
    assert not stale.exists() and keep.exists() and (_upload_dir(env) / "done.jpg").exists()


def test_visible_album_accepts_upload_into_the_open_folder(env):
    folder = env.tmp / "album"
    (folder / "旅行").mkdir(parents=True)
    admin = make_client(env.app, ADMIN)
    space_id = admin.post("/admin/api/spaces", json={"name": "家庭相册", "path": str(folder)}).json()["id"]
    alice = make_client(env.app, ALICE)

    info = alice.get("/api/web/upload/info", params={"space": space_id, "path": "旅行"}).json()
    assert info["space"] == space_id and info["dir"] == "旅行"

    saved = upload(alice, "a.jpg", b"photo", space=space_id, path="旅行")
    assert saved.status_code == 200, saved.text
    assert saved.json()["dir"] == "旅行"
    assert (folder / "旅行" / "a.jpg").read_bytes() == b"photo"

    # 相册根目录、路径穿越、以及指向文件的 path 都不能写到相册外面
    root = upload(alice, "root.txt", b"root", space=space_id)
    assert root.status_code == 200 and (folder / "root.txt").read_bytes() == b"root"
    assert upload(alice, "x.txt", b"x", space=space_id, path="../outside").status_code == 400
    assert not (env.tmp / "outside").exists()
    assert upload(alice, "x.txt", b"x", space=space_id, path="root.txt").status_code == 400


def test_folder_upload_keeps_subfolders_and_personal_space_opens_there(env):
    alice = make_client(env.app, ALICE)
    saved = upload(alice, "a.jpg", b"photo", path="假期/白天")
    assert saved.status_code == 200, saved.text
    assert saved.json()["dir"] == "假期/白天"
    assert (_upload_dir(env) / "假期" / "白天" / "a.jpg").read_bytes() == b"photo"


def test_new_folder_is_one_level_under_the_home_directory(env):
    alice = make_client(env.app, ALICE)
    created = alice.post("/api/folders", json={"name": "假期"})
    assert created.status_code == 200, created.text
    folder = env.settings.home / "假期"
    assert folder.is_dir() and created.json()["name"] == "假期"

    saved = upload(alice, "a.jpg", b"photo", space=created.json()["id"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["dir"] == ""
    assert (folder / "a.jpg").read_bytes() == b"photo"
    assert not (_upload_dir(env) / "a.jpg").exists()

    nested = upload(alice, "b.jpg", b"x", space=created.json()["id"], path="白天")
    assert nested.status_code == 200
    assert (folder / "白天" / "b.jpg").read_bytes() == b"x"

    assert alice.post("/api/folders", json={"name": "假期"}).status_code == 409
    assert alice.post("/api/folders", json={"name": "..\\outside"}).status_code == 400
    assert not (env.tmp / "outside").exists()


def test_personal_folder_stays_in_the_account_and_only_the_owner_uploads(env):
    alice = make_client(env.app, ALICE)
    bob = make_client(env.app, BOB)
    created = alice.post("/api/folders", json={"name": "私密", "kind": "personal"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["kind"] == "personal" and body["writable"] is True and body["name"] == "私密"
    folder = env.settings.accounts_dir / "alice" / "私密"
    assert folder.is_dir()
    assert not (env.settings.home / "私密").exists()

    saved = upload(alice, "a.jpg", b"photo", space=body["id"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["dir"] == ""
    assert (folder / "a.jpg").read_bytes() == b"photo"

    seen = bob.get("/api/spaces").json()["spaces"]
    theirs = next(item for item in seen if item["name"] == "私密")
    assert theirs["kind"] == "personal" and theirs["writable"] is False and theirs["upload_root"] is True
    refused = upload(bob, "b.jpg", b"no", space=body["id"])
    assert refused.status_code == 403
    assert not (folder / "b.jpg").exists()


def test_each_account_uploads_into_its_own_folder(env):
    alice, bob = make_client(env.app, ALICE), make_client(env.app, BOB)
    upload(alice, "a.txt", b"from alice")
    upload(bob, "a.txt", b"from bob")
    assert (_upload_dir(env, "alice") / "a.txt").read_bytes() == b"from alice"
    assert (_upload_dir(env, "bob") / "a.txt").read_bytes() == b"from bob"
