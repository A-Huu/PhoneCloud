"""网页手动上传：multipart 单文件，边收边写盘，不覆盖已有文件。"""

from __future__ import annotations

import errno
import logging
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import BinaryIO

from fastapi import APIRouter, Depends, Request
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from . import fsutil
from .core import AppError, Core, Space, User
from .web_common import current_user, get_core

log = logging.getLogger("phonecloud")
router = APIRouter()

# multipart 的分隔行、头部等额外字节，判断 Content-Length 是否超限时留的余量
_MULTIPART_OVERHEAD = 1024 * 1024


class _UploadReceiver:
    """接收 multipart 解析器的回调，把唯一的文件字段直接写入目标目录下的临时文件。

    不经过内存或系统临时目录：几个 GB 的文件只写一次盘，写完原地改名即可。
    """

    def __init__(self, directory: Path, max_bytes: int) -> None:
        self.directory = directory
        self.max_bytes = max_bytes
        self.temp_path: Path | None = None
        self.filename: str | None = None
        self.size = 0
        self.file_done = False
        self.ended = False
        self._handle: BinaryIO | None = None
        self._in_file_part = False
        self._header_field = b""
        self._header_value = b""
        self._headers: dict[bytes, bytes] = {}

    def callbacks(self) -> dict[str, object]:
        return {
            "on_part_begin": self._on_part_begin,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
            "on_part_data": self._on_part_data,
            "on_part_end": self._on_part_end,
            "on_end": self._on_end,
        }

    def _on_part_begin(self) -> None:
        self._headers = {}
        self._header_field = b""
        self._header_value = b""
        self._in_file_part = False

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _on_header_end(self) -> None:
        self._headers[self._header_field.lower()] = self._header_value
        self._header_field = b""
        self._header_value = b""

    def _on_headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        filename = options.get(b"filename")
        if filename is None:
            return  # 普通表单字段，忽略
        if self.filename is not None:
            raise AppError(400, "一次只能上传一个文件")
        self.filename = filename.decode("utf-8", errors="replace")
        self.temp_path = self.directory / (
            f"{fsutil.UPLOAD_TEMP_PREFIX}{secrets.token_hex(8)}{fsutil.UPLOAD_TEMP_SUFFIX}"
        )
        self._handle = open(self.temp_path, "xb")
        self._in_file_part = True

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if not self._in_file_part or self._handle is None:
            return
        self.size += end - start
        if self.size > self.max_bytes:
            raise AppError(413, f"文件超过单文件上限 {self.max_bytes // (1024 * 1024)} MB")
        self._handle.write(data[start:end])

    def _on_part_end(self) -> None:
        if self._in_file_part and self._handle is not None:
            self._handle.close()
            self._handle = None
            self._in_file_part = False
            self.file_done = True

    def _on_end(self) -> None:
        self.ended = True

    def discard(self) -> None:
        """丢弃未完成的上传，不留半截文件。"""
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self.temp_path is not None:
            try:
                self.temp_path.unlink()
            except OSError:
                pass
            self.temp_path = None


def _existing_ancestor(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _target_space(core: Core, user: User, space_id: int | None) -> Space:
    """不指定空间时进入自己的个人空间。指定了空间则必须看得到，并且这个空间允许上传。"""
    if space_id is None:
        return core.personal_space(user)
    space = core.space_for(user, space_id)  # 无权访问的空间在这里返回 404
    if not space.writable_by(user):
        raise AppError(403, "该空间为只读，不能上传")
    return space


def _subpath(root: Path, rel: str) -> tuple[Path, str]:
    """root 下的相对目录。文件夹可以还不存在（上传整个文件夹时要连目录一起建）。"""
    text = rel.strip().strip("/")
    if not text:
        return root, ""
    try:
        parts = fsutil.split_rel(text)
    except fsutil.PathError as exc:
        raise AppError(400, str(exc)) from None
    directory = root.joinpath(*parts)
    try:
        real = directory.resolve()
        root_real = root.resolve()
    except OSError:
        raise AppError(404, "路径不存在") from None
    if not fsutil.is_within(real, root_real):
        raise AppError(400, "路径超出了空间范围")
    if real.exists() and not real.is_dir():
        raise AppError(400, "只能上传到文件夹里")
    return directory, "/".join(parts)


def _destination(core: Core, space: Space, rel: str) -> tuple[Path, str]:
    """文件直接写进所选文件夹。path 可以带尚不存在的子文件夹。"""
    if core.is_protected(space.root.resolve()):
        raise AppError(404, "路径不存在")
    directory, normalized = _subpath(space.root, rel)
    if core.is_protected(directory.resolve()):
        raise AppError(404, "路径不存在")
    return directory, normalized


@router.get("/api/web/upload/info")
def upload_info(
    user: User = Depends(current_user),
    core: Core = Depends(get_core),
    space: int | None = None,
    path: str = "",
) -> dict[str, object]:
    """上传前的预检信息，前端据此在本地先拦掉超限或磁盘放不下的文件。"""
    target = _target_space(core, user, space)
    directory, rel_dir = _destination(core, target, path)
    free = shutil.disk_usage(_existing_ancestor(directory)).free
    return {
        "space": target.id,
        "dir": rel_dir,
        "max_bytes": core.settings.max_upload_bytes,
        "free_bytes": max(free - core.settings.min_free_bytes, 0),
    }


def _prepare(core: Core, user: User, space_id: int | None, rel: str, content_length: int) -> tuple[Space, Path, str]:
    target = _target_space(core, user, space_id)
    limit = core.settings.max_upload_bytes
    if content_length > limit + _MULTIPART_OVERHEAD:
        raise AppError(413, f"文件超过单文件上限 {core.settings.max_upload_mb} MB")
    directory, rel_dir = _destination(core, target, rel)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(directory).free
    except OSError:
        log.exception("无法创建上传目录 %s", directory)
        raise AppError(500, "电脑上无法创建上传目录") from None
    if free - content_length < core.settings.min_free_bytes:
        raise AppError(507, "电脑磁盘剩余空间不足，无法上传")
    return target, directory, rel_dir


def _finish(receiver: _UploadReceiver, directory: Path, mtime_ms: int | None) -> str:
    assert receiver.temp_path is not None and receiver.filename is not None
    final_name = fsutil.claim_unique(receiver.temp_path, directory, fsutil.sanitize_filename(receiver.filename))
    receiver.temp_path = None
    if mtime_ms is not None and 0 < mtime_ms <= (time.time() + 86400) * 1000:
        # 保留手机上的原始修改时间，电脑上按时间排序照片才有意义
        try:
            os.utime(directory / final_name, (mtime_ms / 1000, mtime_ms / 1000))
        except OSError:
            pass
    return final_name


@router.post("/api/web/upload")
async def web_upload(
    request: Request,
    space: int | None = None,
    path: str = "",
    mtime: int | None = None,
    user: User = Depends(current_user),
    core: Core = Depends(get_core),
) -> dict[str, object]:
    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise AppError(400, "请使用 multipart/form-data 上传")
    try:
        content_length = max(int(request.headers.get("content-length", "0")), 0)
    except ValueError:
        raise AppError(400, "Content-Length 不正确") from None

    personal, directory, rel_dir = await run_in_threadpool(_prepare, core, user, space, path, content_length)

    receiver = _UploadReceiver(directory, core.settings.max_upload_bytes)
    parser = MultipartParser(boundary, receiver.callbacks())  # type: ignore[arg-type]
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > core.settings.max_upload_bytes + _MULTIPART_OVERHEAD:
                raise AppError(413, f"文件超过单文件上限 {core.settings.max_upload_mb} MB")
            # 写盘是阻塞操作，放到线程池里，不卡住同时进行的预览和播放
            await run_in_threadpool(parser.write, chunk)
        parser.finalize()
        if receiver.filename is None:
            raise AppError(400, "请求中没有文件")
        if not (receiver.file_done and receiver.ended):
            raise AppError(400, "上传数据不完整，请重新上传")
        final_name = await run_in_threadpool(_finish, receiver, directory, mtime)
    except ClientDisconnect:
        log.info("账号 %s 的上传中断（已接收 %d 字节）", user.username, received)
        raise AppError(400, "上传已中断") from None
    except MultipartParseError:
        raise AppError(400, "上传数据格式不正确") from None
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise AppError(507, "电脑磁盘剩余空间不足，上传失败") from None
        log.exception("保存上传文件失败")
        raise AppError(500, "电脑上保存文件失败") from None
    finally:
        # 成功时临时文件已改名，这里是空操作；失败或中断时清掉半截文件。
        # 直接同步调用：请求被取消时 finally 里不能再 await。
        receiver.discard()

    original = fsutil.sanitize_filename(receiver.filename)
    log.info("账号 %s 上传 %s/%s（%d 字节）", user.username, rel_dir, final_name, receiver.size)
    return {
        "name": final_name,
        "original_name": original,
        "renamed": final_name != original,
        "size": receiver.size,
        "space": personal.id,
        "dir": rel_dir,
        "path": f"{rel_dir}/{final_name}" if rel_dir else final_name,
    }
