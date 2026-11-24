from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, ALICE, make_client, personal_space_id
from phonecloud import fsutil
from phonecloud.api_user import _RangeNotSatisfiable, parse_range


@pytest.fixture
def shared(env):
    """管理员把一个外部目录建成空间（默认所有登录账号可见），由 alice 来访问。"""
    root = env.tmp / "photos"
    (root / "trip").mkdir(parents=True)
    (root / "trip" / "a.txt").write_bytes(b"0123456789")
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"top secret")

    admin = make_client(env.app, ADMIN)
    space_id = admin.post("/admin/api/spaces", json={"name": "家庭相册", "path": str(root)}).json()["id"]
    env.root, env.outside, env.space_id = root, outside, space_id
    env.client = make_client(env.app, ALICE)
    return env


# ---- 路径穿越 ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../outside/secret.txt",
        "trip/../../outside/secret.txt",
        "..\\outside\\secret.txt",
        "trip/..",
        "C:\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "trip/a.txt:stream",
        "trip/a.txt.",
        "trip/a.txt ",
        "NUL",
        "trip/con.txt",
        "trip/a\x00.txt",
        "trip/a?.txt",
    ],
)
def test_path_traversal_is_rejected_everywhere(shared, path):
    for url in ("/api/fs/list", "/api/fs/stream", "/api/fs/download", "/api/fs/thumb"):
        response = shared.client.get(url, params={"space": shared.space_id, "path": path})
        assert response.status_code == 400, (url, path, response.text)
        assert b"top secret" not in response.content


def test_absolute_and_unc_style_paths_stay_inside_the_space(shared):
    # 以斜杠开头的路径按"空间内的相对路径"处理，而不是磁盘根目录
    for path in ("/trip/a.txt", "\\\\trip\\a.txt", "//trip//a.txt", "./trip/./a.txt"):
        response = shared.client.get("/api/fs/download", params={"space": shared.space_id, "path": path})
        assert response.status_code == 200 and response.content == b"0123456789", path


@pytest.mark.skipif(os.name != "nt", reason="目录联接是 Windows 特性")
def test_junction_pointing_outside_the_space_is_rejected(shared):
    link = shared.root / "escape"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(shared.outside)], capture_output=True)
    if result.returncode != 0:
        pytest.skip("无法创建目录联接")
    try:
        for url, path in (("/api/fs/list", "escape"), ("/api/fs/download", "escape/secret.txt")):
            response = shared.client.get(url, params={"space": shared.space_id, "path": path})
            assert response.status_code == 400
            assert b"top secret" not in response.content
    finally:
        os.rmdir(link)


def test_data_directory_is_never_served_even_if_a_space_contains_it(env):
    """把整个主目录建成空间也读不到数据库（里面有密码哈希）。"""
    admin = make_client(env.app, ADMIN)
    space_id = admin.post("/admin/api/spaces", json={"name": "全部", "path": str(env.settings.home)}).json()["id"]
    alice = make_client(env.app, ALICE)

    names = [e["name"] for e in alice.get("/api/fs/list", params={"space": space_id}).json()["entries"]]
    assert "accounts" in names and "data" not in names
    for url, path in (
        ("/api/fs/list", "data"),
        ("/api/fs/download", "data/phonecloud.db"),
        ("/api/fs/stream", "DATA/phonecloud.db"),
    ):
        assert alice.get(url, params={"space": space_id, "path": path}).status_code == 404

    refused = admin.post("/admin/api/spaces", json={"name": "数据", "path": str(env.settings.data_dir)})
    assert refused.status_code == 400


# ---- 目录浏览 ---------------------------------------------------------


def test_listing_sorts_folders_first_and_names_naturally(shared):
    folder = shared.root / "mix"
    folder.mkdir()
    for name in ("IMG_10.jpg", "IMG_2.jpg", "img_1.JPG", "b.mp4", "notes.txt"):
        (folder / name).write_bytes(b"x")
    (folder / "zdir").mkdir()
    (folder / ".upload-abcdef.part").write_bytes(b"partial")

    body = shared.client.get("/api/fs/list", params={"space": shared.space_id, "path": "mix"}).json()
    assert body["path"] == "mix"
    assert body["space"] == {
        "id": shared.space_id, "name": "家庭相册", "kind": "shared", "writable": True, "upload_root": True,
    }
    assert [e["name"] for e in body["entries"]] == ["zdir", "b.mp4", "img_1.JPG", "IMG_2.jpg", "IMG_10.jpg", "notes.txt"]
    by_name = {e["name"]: e for e in body["entries"]}
    assert by_name["zdir"]["type"] == "dir"
    assert by_name["IMG_2.jpg"] == {
        "name": "IMG_2.jpg", "type": "file", "size": 1, "mtime": by_name["IMG_2.jpg"]["mtime"],
        "kind": "image", "thumb": True,
    }
    assert by_name["b.mp4"]["kind"] == "video" and by_name["b.mp4"]["thumb"] is False
    assert by_name["notes.txt"]["kind"] == "other"


def test_listing_missing_folder_and_file_as_folder(shared):
    assert shared.client.get("/api/fs/list", params={"space": shared.space_id, "path": "nope"}).status_code == 404
    assert shared.client.get("/api/fs/list", params={"space": shared.space_id, "path": "trip/a.txt"}).status_code == 404
    assert shared.client.get("/api/fs/download", params={"space": shared.space_id, "path": "trip"}).status_code == 404


def test_upload_temp_files_cannot_be_downloaded(shared):
    (shared.root / ".upload-abcdef.part").write_bytes(b"partial")
    response = shared.client.get("/api/fs/download", params={"space": shared.space_id, "path": ".upload-abcdef.part"})
    assert response.status_code == 404


# ---- 播放 / 下载 ------------------------------------------------------


@pytest.fixture
def video(shared):
    data = bytes(range(256)) * 40  # 10240 字节
    (shared.root / "clip.mp4").write_bytes(data)
    shared.data = data
    shared.params = {"space": shared.space_id, "path": "clip.mp4"}
    return shared


def test_stream_whole_file(video):
    response = video.client.get("/api/fs/stream", params=video.params)
    assert response.status_code == 200
    assert response.content == video.data
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "10240"
    assert response.headers["content-disposition"].startswith("inline;")


@pytest.mark.parametrize(
    "header, start, end",
    [
        ("bytes=0-99", 0, 99),
        ("bytes=100-", 100, 10239),
        ("bytes=-500", 9740, 10239),
        ("bytes=10000-99999", 10000, 10239),
        ("bytes=10239-10239", 10239, 10239),
    ],
)
def test_stream_range_requests_allow_seeking(video, header, start, end):
    response = video.client.get("/api/fs/stream", params=video.params, headers={"Range": header})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes {start}-{end}/10240"
    assert response.headers["content-length"] == str(end - start + 1)
    assert response.content == video.data[start : end + 1]


def test_stream_unsatisfiable_range(video):
    response = video.client.get("/api/fs/stream", params=video.params, headers={"Range": "bytes=10240-"})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10240"


def test_stream_if_range_and_conditional_get(video):
    etag = video.client.get("/api/fs/stream", params=video.params).headers["etag"]
    resumed = video.client.get("/api/fs/stream", params=video.params, headers={"Range": "bytes=5-9", "If-Range": etag})
    assert resumed.status_code == 206
    changed = video.client.get(
        "/api/fs/stream", params=video.params, headers={"Range": "bytes=5-9", "If-Range": '"stale"'}
    )
    assert changed.status_code == 200 and changed.content == video.data
    not_modified = video.client.get("/api/fs/stream", params=video.params, headers={"If-None-Match": etag})
    assert not_modified.status_code == 304 and not_modified.content == b""


def test_parse_range_edge_cases():
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-0", 0) is None  # 空文件直接回 200
    assert parse_range("bytes=0-10,20-30", 100) is None  # 多段：忽略
    assert parse_range("items=0-10", 100) is None
    assert parse_range("bytes=-", 100) is None
    assert parse_range("bytes=-1000", 100) == (0, 99)
    for bad in ("bytes=100-", "bytes=50-10", "bytes=-0"):
        with pytest.raises(_RangeNotSatisfiable):
            parse_range(bad, 100)


def test_download_uses_attachment_with_unicode_filename(shared):
    (shared.root / "全家福 2026.jpg").write_bytes(b"jpegdata")
    response = shared.client.get("/api/fs/download", params={"space": shared.space_id, "path": "全家福 2026.jpg"})
    assert response.status_code == 200 and response.content == b"jpegdata"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''%E5%85%A8%E5%AE%B6%E7%A6%8F%202026.jpg" in disposition
    partial = shared.client.get(
        "/api/fs/download", params={"space": shared.space_id, "path": "全家福 2026.jpg"}, headers={"Range": "bytes=4-"}
    )
    assert partial.status_code == 206 and partial.content == b"data"


@pytest.mark.parametrize("name", ["page.html", "drawing.svg", "script.js", "doc.pdf"])
def test_stream_never_renders_active_content_inline(shared, name):
    """HTML/SVG 若被浏览器直接打开，其中的脚本能拿到登录态，必须强制下载。"""
    (shared.root / name).write_bytes(b"<script>alert(1)</script>")
    response = shared.client.get("/api/fs/stream", params={"space": shared.space_id, "path": name})
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"


# ---- 缩略图 -----------------------------------------------------------


def _thumb_files(env) -> list[Path]:
    return [p for p in env.settings.thumbs_dir.rglob("*.jpg")]


def test_thumbnail_is_generated_cached_and_refreshed(shared):
    source = shared.root / "big.jpg"
    Image.new("RGB", (4000, 3000), (200, 30, 30)).save(source, "JPEG")
    params = {"space": shared.space_id, "path": "big.jpg"}

    first = shared.client.get("/api/fs/thumb", params=params)
    assert first.status_code == 200 and first.headers["content-type"] == "image/jpeg"
    thumb_path = shared.tmp / "thumb.jpg"
    thumb_path.write_bytes(first.content)
    with Image.open(thumb_path) as image:
        assert image.size == (360, 270)
    assert len(first.content) < source.stat().st_size
    files = _thumb_files(shared)
    assert len(files) == 1

    # 第二次直接走缓存：缓存文件没有被重写
    before = files[0].stat().st_mtime_ns
    second = shared.client.get("/api/fs/thumb", params=params)
    assert second.content == first.content and files[0].stat().st_mtime_ns == before
    cached = shared.client.get("/api/fs/thumb", params=params, headers={"If-None-Match": first.headers["etag"]})
    assert cached.status_code == 304

    # 源文件变了：重新生成，旧缓存被清掉
    Image.new("RGB", (1000, 2000), (30, 30, 200)).save(source, "JPEG")
    third = shared.client.get("/api/fs/thumb", params=params)
    thumb_path.write_bytes(third.content)
    with Image.open(thumb_path) as image:
        assert image.size == (180, 360)
    assert len(_thumb_files(shared)) == 1 and _thumb_files(shared) != files
    with shared.core.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM thumb_cache").fetchone()[0] == 1


def test_thumbnail_respects_exif_orientation(shared):
    """手机竖拍的照片像素是横的，靠 EXIF 标记旋转；缩略图必须是竖的。"""
    source = shared.root / "portrait.jpg"
    image = Image.new("RGB", (800, 600), (10, 200, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # 需顺时针旋转 90 度显示
    image.save(source, "JPEG", exif=exif)
    response = shared.client.get("/api/fs/thumb", params={"space": shared.space_id, "path": "portrait.jpg"})
    out = shared.tmp / "t.jpg"
    out.write_bytes(response.content)
    with Image.open(out) as thumb:
        assert thumb.size == (270, 360)


def test_thumbnail_of_transparent_png(shared):
    Image.new("RGBA", (500, 500), (0, 0, 0, 0)).save(shared.root / "logo.png")
    response = shared.client.get("/api/fs/thumb", params={"space": shared.space_id, "path": "logo.png"})
    assert response.status_code == 200
    out = shared.tmp / "t.jpg"
    out.write_bytes(response.content)
    with Image.open(out) as thumb:
        assert thumb.getpixel((10, 10)) == (255, 255, 255)  # 透明处铺白底而不是变黑


def test_thumbnail_unsupported_or_corrupt(shared):
    (shared.root / "movie.mp4").write_bytes(b"not an image")
    (shared.root / "photo.heic").write_bytes(b"heic")
    (shared.root / "broken.jpg").write_bytes(b"this is not a jpeg")
    for name in ("movie.mp4", "photo.heic", "broken.jpg"):
        response = shared.client.get("/api/fs/thumb", params={"space": shared.space_id, "path": name})
        assert response.status_code == 415, name
    assert _thumb_files(shared) == []
    assert shared.client.get("/api/fs/thumb", params={"space": shared.space_id, "path": "nope.jpg"}).status_code == 404


# ---- 文件名工具 -------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("IMG_001.jpg", "IMG_001.jpg"),
        ("../../evil.txt", "evil.txt"),
        ("C:\\Users\\me\\photo.png", "photo.png"),
        ("a<b>c:d|e?.txt", "a_b_c_d_e_.txt"),
        ("  spaced.txt  ", "spaced.txt"),
        ("trailing.dots...", "trailing.dots"),
        ("", "file"),
        ("...", "file"),
        ("CON.txt", "_CON.txt"),
        ("nul", "_nul"),
        (".upload-1234.part", "_.upload-1234.part"),
        ("中文 文件.mp4", "中文 文件.mp4"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert fsutil.sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_but_keeps_extension():
    name = fsutil.sanitize_filename("x" * 500 + ".jpeg")
    assert len(name) == 180 and name.endswith(".jpeg")


def test_claim_unique_never_overwrites(tmp_path):
    (tmp_path / "IMG_001.jpg").write_bytes(b"original")
    names = []
    for i in range(3):
        temp = tmp_path / f".upload-{i}.part"
        temp.write_bytes(f"new{i}".encode())
        names.append(fsutil.claim_unique(temp, tmp_path, "IMG_001.jpg"))
        assert not temp.exists()
    assert names == ["IMG_001_1.jpg", "IMG_001_2.jpg", "IMG_001_3.jpg"]
    assert (tmp_path / "IMG_001.jpg").read_bytes() == b"original"
    assert (tmp_path / "IMG_001_2.jpg").read_bytes() == b"new1"

    # 没有扩展名、多个点的文件名
    for name, expected in (("README", "README_1"), ("a.tar.gz", "a.tar_1.gz")):
        (tmp_path / name).write_bytes(b"x")
        temp = tmp_path / ".upload-x.part"
        temp.write_bytes(b"y")
        assert fsutil.claim_unique(temp, tmp_path, name) == expected


def test_personal_space_root_is_created_on_demand(env):
    alice = make_client(env.app, ALICE)
    root = env.settings.accounts_dir / "alice"
    import shutil

    shutil.rmtree(root)
    response = alice.get("/api/fs/list", params={"space": personal_space_id(alice)})
    assert response.status_code == 200 and response.json()["entries"] == []
    assert root.is_dir()
