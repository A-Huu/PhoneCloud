"""HTTP 层公用部分：鉴权依赖、安全相关中间件。"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Depends, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .core import AppError, Core, User

COOKIE_NAME = "pc_token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_core(request: Request) -> Core:
    return request.app.state.core


def extract_token(request: Request) -> str | None:
    """App 等客户端用 Authorization: Bearer；浏览器用登录时种下的 Cookie
    （<img>/<video> 发请求时带不了自定义请求头，只能靠 Cookie）。"""
    authorization = request.headers.get("authorization", "")
    if authorization[:7].lower() == "bearer ":
        return authorization[7:].strip() or None
    return request.cookies.get(COOKIE_NAME)


def current_user(request: Request, core: Core = Depends(get_core)) -> User:
    token = extract_token(request)
    user = core.user_for_token(token) if token else None
    if user is None:
        raise AppError(401, "未登录或登录已过期")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise AppError(403, "需要管理员权限")
    return user


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_loopback(request: Request) -> bool:
    return client_ip(request) in ("127.0.0.1", "::1")


class SecurityMiddleware:
    """纯 ASGI 中间件（不缓冲请求体，大文件上传照常流式处理）。

    1. 拒绝跨站发起的写请求：登录态在 Cookie 里，别的网页不应能替用户发 POST。
    2. 给所有响应补上基础安全头和缓存策略。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] not in _SAFE_METHODS:
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            if origin is not None and urlsplit(origin).netloc.lower() != headers.get("host", "").lower():
                response = JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)
                await response(scope, receive, send)
                return

        path: str = scope["path"]
        is_api = path.startswith("/api/") or path.startswith("/admin/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                # 接口默认不缓存；页面和静态资源每次向服务器确认，升级 exe 后立即生效
                headers.setdefault("Cache-Control", "no-store" if is_api else "no-cache")
            await send(message)

        await self.app(scope, receive, send_with_headers)
