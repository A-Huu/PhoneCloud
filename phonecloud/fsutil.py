"""文件系统工具：路径穿越防护、文件名清洗、重名处理、类型识别。"""

from __future__ import annotations

import itertools
import os
import re
import stat as stat_module
import unicodedata
from pathlib import Path

UPLOAD_TEMP_PREFIX = ".upload-"
UPLOAD_TEMP_SUFFIX = ".part"

_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
_FORBIDDEN_CHARS = '<>:"|?*'
_MAX_FILENAME_LENGTH = 180

# Pillow 不装插件就能解码的格式；HEIC 等只预览原图，不生成缩略图。
THUMB_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".3gp": "video/3gpp",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}


class PathError(Exception):
    """请求的路径不合法或越出了空间根目录。"""


def kind_of(name: str) -> str:
    """按扩展名归类：image / video / audio / other。"""
    mime = _MIME_TYPES.get(os.path.splitext(name)[1].lower())
    return mime.split("/")[0] if mime else "other"


def mime_of(name: str) -> str:
    return _MIME_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def can_thumbnail(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in THUMB_EXTS


def is_upload_temp(name: str) -> bool:
    return name.startswith(UPLOAD_TEMP_PREFIX) and name.endswith(UPLOAD_TEMP_SUFFIX)


def split_rel(rel: str) -> list[str]:
    """把客户端传来的相对路径拆成逐级名称，任何可疑成分都直接拒绝。"""
    if "\x00" in rel:
        raise PathError("路径包含非法字符")
    parts = [p for p in re.split(r"[\\/]+", rel) if p not in ("", ".")]
    for part in parts:
        if part == "..":
            raise PathError("路径不能包含 ..")
        if any(c in _FORBIDDEN_CHARS or ord(c) < 32 for c in part):
            # 冒号同时挡住了盘符（C:）和 NTFS 备用数据流（file:stream）
            raise PathError("路径包含非法字符")
        if part != part.rstrip(" ."):
            # Windows 会悄悄去掉结尾的点和空格，"a." 与 "a" 是同一个文件
            raise PathError("路径包含非法名称")
        if part.split(".")[0].upper() in _RESERVED_NAMES:
            raise PathError("路径包含系统保留名称")
    return parts


def resolve_in(root: Path, rel: str) -> tuple[Path, str]:
    """把 rel 解析为 root 之内的真实路径，返回（真实路径，规范化的相对路径）。

    解析会展开符号链接和目录联接，所以指向根目录之外的链接也会被拒绝。
    """
    parts = split_rel(rel)
    root_real = root.resolve()
    real = root_real.joinpath(*parts).resolve()
    if not is_within(real, root_real):
        raise PathError("路径超出了空间范围")
    return real, "/".join(parts)


def is_within(path: Path, parent: Path) -> bool:
    """path 是否等于 parent 或位于其下（两者都应已 resolve）。"""
    return path == parent or parent in path.parents


def sanitize_filename(name: str) -> str:
    """把上传时客户端给的文件名变成可以安全落盘的单级文件名。"""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join("_" if (c in _FORBIDDEN_CHARS or ord(c) < 32) else c for c in name)
    name = name.strip().rstrip(" .")
    if not name:
        name = "file"
    stem, ext = os.path.splitext(name)
    if len(ext) > 20:  # 不像是扩展名，整体当作主名
        stem, ext = name, ""
    if stem.split(".")[0].upper() in _RESERVED_NAMES or is_upload_temp(name):
        stem = "_" + stem
    stem = stem[: _MAX_FILENAME_LENGTH - len(ext)]
    return stem + ext


def claim_unique(temp_path: Path, directory: Path, filename: str) -> str:
    """把临时文件改名为 directory 下不冲突的名字，返回最终文件名。

    同名时依次尝试 name_1.ext、name_2.ext……已有文件永远不会被覆盖；
    "检查是否存在"和"占用名字"是同一个原子操作，并发上传同名文件也不会互相覆盖。
    """
    stem, ext = os.path.splitext(filename)
    for index in itertools.count():
        if index > 100_000:
            raise OSError("同名文件过多，无法生成不冲突的文件名")
        candidate = filename if index == 0 else f"{stem}_{index}{ext}"
        target = directory / candidate
        try:
            if os.name == "nt":
                os.rename(temp_path, target)  # Windows 上目标已存在会失败
            else:
                os.link(temp_path, target)  # POSIX 的 rename 会覆盖，改用硬链接占名
                os.unlink(temp_path)
        except FileExistsError:
            continue
        return candidate
    raise AssertionError("unreachable")


def _natural_key(name: str) -> list[object]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.casefold())]


def list_directory(directory: Path, *, exclude: Path | None = None) -> list[dict[str, object]]:
    """列出目录内容：文件夹在前，按名称自然排序。跳过系统文件、上传临时文件和 exclude。"""
    entries: list[dict[str, object]] = []
    with os.scandir(directory) as it:
        for entry in it:
            if is_upload_temp(entry.name):
                continue
            try:
                st = entry.stat()
                is_dir = entry.is_dir()
            except OSError:
                continue  # 无权限或已被删除
            attrs = getattr(st, "st_file_attributes", 0)
            if attrs & stat_module.FILE_ATTRIBUTE_SYSTEM:
                continue  # desktop.ini、$RECYCLE.BIN、System Volume Information 等
            if exclude is not None and is_dir and Path(entry.path) == exclude:
                continue
            if is_dir:
                entries.append({"name": entry.name, "type": "dir", "mtime": int(st.st_mtime)})
            else:
                entries.append(
                    {
                        "name": entry.name,
                        "type": "file",
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                        "kind": kind_of(entry.name),
                        "thumb": can_thumbnail(entry.name),
                    }
                )
    entries.sort(key=lambda e: (e["type"] != "dir", _natural_key(str(e["name"]))))
    return entries


def etag_for(st: os.stat_result) -> str:
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
