"""管理页接口：首次初始化、账号、空间、授权、磁盘概览。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from . import __version__, winutil
from .core import AppError, Core, User
from .web_common import admin_user, extract_token, get_core, is_loopback

router = APIRouter(prefix="/admin/api")


class Credentials(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordBody(BaseModel):
    password: str = Field(max_length=256)


class SpaceBody(BaseModel):
    name: str = Field(max_length=100)
    # 留空表示默认位置：主目录下与空间同名的文件夹
    path: str = Field(default="", max_length=1024)


class VisibilityBody(BaseModel):
    visibility: str = Field(max_length=16)


class GrantBody(BaseModel):
    space_id: int
    user_id: int
    # 页面上看到的可见范围；与实际不一致时拒绝，避免在过期页面上把"不可见"加成"可见"
    visibility: str | None = Field(default=None, max_length=16)


# ---- 首次初始化 -------------------------------------------------------


@router.get("/state")
def state(request: Request, core: Core = Depends(get_core)) -> dict[str, object]:
    needs_setup = not core.has_admin()
    return {
        "needs_setup": needs_setup,
        "setup_allowed": needs_setup and is_loopback(request),
        "port": core.settings.port,
    }


@router.post("/setup")
def setup(body: Credentials, request: Request, core: Core = Depends(get_core)) -> dict[str, object]:
    """创建第一个管理员。只接受本机访问，防止局域网里别人抢先初始化。"""
    if core.has_admin():
        raise AppError(409, "管理员已存在，请直接登录")
    if not is_loopback(request):
        raise AppError(403, "首次初始化只能在运行 PhoneCloud 的电脑上进行")
    core.create_user(body.username, body.password, is_admin=True)
    return {"ok": True}


# ---- 账号 -------------------------------------------------------------


@router.get("/users")
def list_users(_: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    return {"users": core.list_users()}


@router.post("/users")
def create_user(body: Credentials, _: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    user = core.create_user(body.username, body.password)
    return {"id": user.id, "username": user.username}


@router.post("/users/{user_id}/disable")
def disable_user(user_id: int, admin: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    core.set_disabled(user_id, True, acting=admin)
    return {"ok": True}


@router.post("/users/{user_id}/enable")
def enable_user(user_id: int, admin: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    core.set_disabled(user_id, False, acting=admin)
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    core.delete_user(user_id, acting=admin)
    return {"ok": True}


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: int,
    body: PasswordBody,
    request: Request,
    admin: User = Depends(admin_user),
    core: Core = Depends(get_core),
) -> dict[str, object]:
    # 管理员改自己的密码时保留当前这次登录，其余登录态全部失效
    keep = extract_token(request) if user_id == admin.id else None
    core.set_password(user_id, body.password, keep_token=keep)
    return {"ok": True}


# ---- 空间与授权 -------------------------------------------------------


@router.get("/spaces")
def list_spaces(_: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    # default_root：创建空间时文件夹路径的默认位置，管理页据此预填"主目录\空间名称"
    return {"spaces": core.list_spaces_admin(), "default_root": str(core.settings.home)}


@router.post("/spaces")
def create_space(body: SpaceBody, _: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    return {"id": core.create_space(body.name, body.path)}


@router.delete("/spaces/{space_id}")
def delete_space(space_id: int, _: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    core.delete_space(space_id)
    return {"ok": True}


@router.post("/spaces/{space_id}/visibility")
def set_space_visibility(
    space_id: int, body: VisibilityBody, _: User = Depends(admin_user), core: Core = Depends(get_core)
) -> dict[str, object]:
    core.set_visibility(space_id, body.visibility)
    return {"ok": True}


@router.post("/grants")
def create_grant(body: GrantBody, _: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    return {"id": core.grant(body.space_id, body.user_id, expected_visibility=body.visibility)}


@router.delete("/grants/{grant_id}")
def delete_grant(grant_id: int, _: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    core.revoke(grant_id)
    return {"ok": True}


# ---- 概览 -------------------------------------------------------------


@router.get("/overview")
def overview(_: User = Depends(admin_user), core: Core = Depends(get_core)) -> dict[str, object]:
    settings = core.settings
    urls = [f"http://{ip}:{settings.port}" for ip in winutil.lan_addresses()]
    # 每个地址一张二维码，顺序与 urls 相同；第一张是无线网卡
    qr_svgs = [winutil.qr_svg(url) for url in urls]
    return {
        "version": __version__,
        "home": str(settings.home),
        "port": settings.port,
        "urls": urls,
        "qr_svgs": qr_svgs,
        "qr_svg": qr_svgs[0] if qr_svgs else "",
        "firewall_command": winutil.firewall_command(settings.port),
        "private_network_command": winutil.private_network_command(),
        "max_upload_mb": settings.max_upload_mb,
        "min_free_mb": settings.min_free_mb,
        "disks": core.disk_overview(),
    }
