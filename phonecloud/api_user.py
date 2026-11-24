"""用户端接口：登录、空间列表、目录浏览、缩略图、播放与下载。"""

from __future__ import annotations

import logging
import os
import re
import stat as stat_module
from email.utils import formatdate
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, StreamingResponse

from . import APP_NAME, __version__, fsutil, thumbs, winutil
from .core import AppError, Core, Space, User
from .web_common import COOKIE_NAME, client_ip, current_user, extract_token, get_core

log = logging.getLogger("phonecloud")
router = APIRouter()

_CHUNK_SIZE = 256 * 1024
_COOKIE_MAX_AGE = 400 * 86400  # 浏览器允许的上限；真正的有效期由服务端会话决定
_RANGE_RE = re.compile(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*")


class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class TrashBody(BaseModel):
    space: int
    path: str = Field(min_length=1, max_length=1024)


class FolderBody(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    kind: str = "shared"


def user_json(user: User) -> dict[str, object]:
    return {"id": user.id, "username": user.username, "is_admin": user.is_admin}


def space_json(space: Space, user: User) -> dict[str, object]:
    data: dict[str, object] = {
        "id": space.id,
        "name": space.display_name(user),
        "kind": space.kind,
        "writable": space.writable_by(user),
        # 更早的库里没有 path 的个人空间，根目录是整个账号目录，不作为上传目标。
        "upload_root": not space.account_space,
    }
    return data


# ---- 登录 -------------------------------------------------------------


# 手机桌面上的页面和本站不是同一个来源，只对这一处放开跨源读取。
_PING_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Private-Network": "true",
    "Cache-Control": "no-store",
}


@router.options("/api/ping")
def ping_options() -> Response:
    return Response(
        status_code=204,
        headers={
            **_PING_HEADERS,
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        },
    )


@router.get("/api/ping")
def ping() -> JSONResponse:
    return JSONResponse({"ok": True, "name": APP_NAME, "version": __version__}, headers=_PING_HEADERS)


@router.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response, core: Core = Depends(get_core)) -> dict[str, object]:
    throttle_key = f"{client_ip(request)}|{body.username.strip().lower()}"
    if core.throttle.blocked(throttle_key):
        raise AppError(429, "登录失败次数过多，请 10 分钟后再试")
    result = core.login(body.username, body.password)
    if result is None:
        core.throttle.record_failure(throttle_key)
        raise AppError(401, "账号或密码错误")
    core.throttle.reset(throttle_key)
    token, expires_at, user = result
    response.set_cookie(COOKIE_NAME, token, max_age=_COOKIE_MAX_AGE, httponly=True, samesite="strict", path="/")
    return {"token": token, "expires_at": expires_at, "user": user_json(user)}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, core: Core = Depends(get_core)) -> dict[str, object]:
    token = extract_token(request)
    if token:
        core.logout(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: User = Depends(current_user)) -> dict[str, object]:
    return {"user": user_json(user), "version": __version__}


# ---- 空间与目录 -------------------------------------------------------


@router.get("/api/spaces")
def list_spaces(user: User = Depends(current_user), core: Core = Depends(get_core)) -> dict[str, object]:
    return {"spaces": [space_json(s, user) for s in core.spaces_for(user)]}


@router.post("/api/folders")
def create_folder(body: FolderBody, user: User = Depends(current_user), core: Core = Depends(get_core)) -> dict[str, object]:
    """上传时新建文件夹。kind=shared 放在主目录，能看就能上传；kind=personal 放在自己的账号目录，只有自己能上传。"""
    space_id = core.create_folder(user, body.name, body.kind)
    return space_json(core.space_for(user, space_id), user)


@router.get("/api/fs/list")
def fs_list(
    space: int, path: str = "", user: User = Depends(current_user), core: Core = Depends(get_core)
) -> dict[str, object]:
    sp = core.space_for(user, space)
    real, rel = core.resolve(sp, path)
    if not rel and sp.kind == "personal":
        # 个人空间的根目录按需创建，新账号还没上传过也能正常打开
        try:
            real.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    if not real.is_dir():
        if not rel:
            raise AppError(404, "该空间对应的文件夹当前不可用（磁盘可能未连接）")
        raise AppError(404, "文件夹不存在")
    try:
        entries = fsutil.list_directory(real, exclude=core.settings.data_dir.resolve())
    except PermissionError:
        raise AppError(403, "电脑上的这个文件夹没有读取权限") from None
    except OSError:
        raise AppError(404, "文件夹不存在") from None
    return {"space": space_json(sp, user), "path": rel, "entries": entries}


# ---- 文件内容 ---------------------------------------------------------


def _locate_file(core: Core, user: User, space_id: int, path: str) -> tuple[Space, Path, str, os.stat_result]:
    sp = core.space_for(user, space_id)
    real, rel = core.resolve(sp, path)
    if fsutil.is_upload_temp(real.name):
        raise AppError(404, "文件不存在")
    try:
        st = real.stat()
    except OSError:
        raise AppError(404, "文件不存在") from None
    if not stat_module.S_ISREG(st.st_mode):
        raise AppError(404, "文件不存在")
    return sp, real, rel, st


def _content_disposition(disposition: str, filename: str) -> str:
    # 响应头只能是 ASCII：给老客户端一个替代名，再用 RFC 5987 的 filename* 带上真实的中文名
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


class _RangeNotSatisfiable(Exception):
    def __init__(self, size: int) -> None:
        self.size = size


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析单段 Range，返回闭区间 (start, end)；返回 None 表示应回整个文件。

    多段或格式不对的 Range 按规范可以忽略，直接回整个文件。范围无法满足时抛 416。
    """
    if not header or size == 0:
        return None
    match = _RANGE_RE.fullmatch(header)
    if not match or match.group(1) == match.group(2) == "":
        return None
    first, last = match.groups()
    if first == "":  # bytes=-N：最后 N 字节
        length = int(last)
        if length == 0:
            raise _RangeNotSatisfiable(size)
        return max(size - length, 0), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise _RangeNotSatisfiable(size)
    return start, end


def _iter_file(handle: BinaryIO, start: int, length: int) -> Iterator[bytes]:
    try:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            data = handle.read(min(_CHUNK_SIZE, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
    finally:
        handle.close()


def _file_response(request: Request, path: Path, st: os.stat_result, *, attachment: bool) -> Response:
    name = path.name
    # 只有图片/视频/音频才允许在浏览器里直接打开。HTML、SVG 之类如果内联展示，
    # 其中的脚本会以本站身份运行并拿到登录态，所以一律按附件下载。
    inline = not attachment and fsutil.kind_of(name) in ("image", "video", "audio")
    etag = fsutil.etag_for(st)
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": formatdate(st.st_mtime, usegmt=True),
        "Cache-Control": "private, no-cache",
        "Content-Disposition": _content_disposition("inline" if inline else "attachment", name),
    }

    range_header = request.headers.get("range")
    if range_header is None and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    if_range = request.headers.get("if-range")
    if if_range is not None and if_range != etag:
        range_header = None  # 文件已变化，续传的前半段作废，重新回整个文件

    try:
        byte_range = parse_range(range_header, st.st_size)
    except _RangeNotSatisfiable as exc:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{exc.size}"})

    try:
        handle = open(path, "rb")
    except PermissionError:
        raise AppError(403, "电脑上的这个文件没有读取权限") from None
    except OSError:
        raise AppError(404, "文件不存在") from None

    if byte_range is None:
        start, length, status = 0, st.st_size, 200
    else:
        start, end = byte_range
        length, status = end - start + 1, 206
        headers["Content-Range"] = f"bytes {start}-{end}/{st.st_size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(
        _iter_file(handle, start, length), status_code=status, headers=headers, media_type=fsutil.mime_of(name)
    )


@router.get("/api/fs/stream")
def fs_stream(
    request: Request, space: int, path: str, user: User = Depends(current_user), core: Core = Depends(get_core)
) -> Response:
    """在线预览/播放：支持 Range，视频可拖动进度；不转码，原样输出。"""
    _, real, _, st = _locate_file(core, user, space, path)
    return _file_response(request, real, st, attachment=False)


@router.get("/api/fs/download")
def fs_download(
    request: Request, space: int, path: str, user: User = Depends(current_user), core: Core = Depends(get_core)
) -> Response:
    _, real, _, st = _locate_file(core, user, space, path)
    return _file_response(request, real, st, attachment=True)


@router.get("/api/fs/thumb")
def fs_thumb(
    request: Request, space: int, path: str, user: User = Depends(current_user), core: Core = Depends(get_core)
) -> Response:
    sp, real, rel, _ = _locate_file(core, user, space, path)
    if not fsutil.can_thumbnail(real.name):
        raise AppError(415, "该类型的文件不支持缩略图")
    thumb = thumbs.get_thumbnail(core, sp, rel, real)
    etag = f'"{thumb.stem}"'
    # 前端在地址里带了源文件的修改时间，源文件变化后地址随之变化，所以可以放心长时间缓存
    headers = {"ETag": etag, "Cache-Control": "private, max-age=604800"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(thumb.read_bytes(), media_type="image/jpeg", headers=headers)


@router.post("/api/fs/trash")
def fs_trash(body: TrashBody, user: User = Depends(current_user), core: Core = Depends(get_core)) -> dict[str, object]:
    """把文件或文件夹移到电脑的回收站。能上传的地方才能删；别人的个人空间仍然只能看。"""
    space = core.space_for(user, body.space)
    if not space.writable_by(user):
        raise AppError(403, "这里只能看，不能删除")
    real, rel = core.resolve(space, body.path)
    if not rel or real.resolve() == space.root.resolve():
        raise AppError(400, "不能删除整个空间")
    if not real.exists():
        raise AppError(404, "文件不存在")
    try:
        winutil.recycle(real)
    except OSError:
        log.exception("移到回收站失败 %s", real)
        raise AppError(500, "无法把这个文件移到回收站") from None
    return {"ok": True, "path": rel}
