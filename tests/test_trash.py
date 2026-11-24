"""删除文件：能上传的地方可以删，文件进入 Windows 回收站，不直接抹掉。"""

from __future__ import annotations

from conftest import ALICE, BOB, make_client, personal_space_id, upload
from phonecloud import winutil


def test_owner_can_move_a_file_and_a_folder_to_the_recycle_bin(env):
    alice = make_client(env.app, ALICE)
    space = personal_space_id(alice)
    saved = upload(alice, "keep.txt", b"data").json()
    folder = upload(alice, "inside.txt", b"x", path="要删的").json()

    removed = alice.post("/api/fs/trash", json={"space": space, "path": saved["path"]})
    assert removed.status_code == 200, removed.text
    assert not (_file(env, saved["path"])).exists()

    removed_dir = alice.post("/api/fs/trash", json={"space": space, "path": folder["dir"]})
    assert removed_dir.status_code == 200, removed_dir.text
    assert not (_file(env, folder["dir"])).exists()

    # 不能删掉整个个人空间
    assert alice.post("/api/fs/trash", json={"space": space, "path": ""}).status_code == 400
    assert alice.post("/api/fs/trash", json={"space": space, "path": "."}).status_code in (400, 422)


def test_recycle_bin_actually_receives_the_file(env, tmp_path):
    target = tmp_path / "phonecloud-recycle-probe.txt"
    target.write_text("probe", encoding="utf-8")
    winutil.recycle(target)
    assert not target.exists()


def test_someone_elses_personal_files_cannot_be_trashed(env):
    alice = make_client(env.app, ALICE)
    bob = make_client(env.app, BOB)
    saved = upload(alice, "mine.txt", b"secret").json()
    refused = bob.post("/api/fs/trash", json={"space": saved["space"], "path": saved["path"]})
    assert refused.status_code == 403
    assert (_file(env, saved["path"])).read_bytes() == b"secret"


def test_a_visible_album_can_be_cleaned_by_anyone_who_can_see_it(env):
    from conftest import ADMIN

    folder = env.tmp / "album"
    folder.mkdir()
    (folder / "old.txt").write_bytes(b"old")
    admin = make_client(env.app, ADMIN)
    space_id = admin.post("/admin/api/spaces", json={"name": "家庭相册", "path": str(folder)}).json()["id"]
    bob = make_client(env.app, BOB)
    removed = bob.post("/api/fs/trash", json={"space": space_id, "path": "old.txt"})
    assert removed.status_code == 200, removed.text
    assert not (folder / "old.txt").exists()
    assert bob.post("/api/fs/trash", json={"space": space_id, "path": ""}).status_code == 400


def _file(env, rel: str):
    return env.settings.accounts_dir.joinpath("alice", "alice", *rel.split("/"))
