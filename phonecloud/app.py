"""组装 FastAPI 应用：接口路由、内嵌网页、统一错误格式。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import APP_NAME, __version__, api_admin, api_upload, api_user
from .config import Settings
from .core import AppError, Core
from .web_common import SecurityMiddleware

log = logging.getLogger("phonecloud")

WEB_DIR = Path(__file__).resolve().parent / "web"

# 页面只加载本站的脚本和样式，不允许内联脚本：即便文件名等内容里混入了标签，也无法执行。
_PAGE_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def create_app(settings: Settings) -> FastAPI:
    core = Core(settings)
    core.init()

    # 不开放 /docs：它依赖外网 CDN，局域网离线环境打不开
    app = FastAPI(title=APP_NAME, version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.core = core
    app.add_middleware(SecurityMiddleware)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "请求参数不正确"}, status_code=400)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("处理 %s %s 时出错", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"detail": "服务器内部错误"}, status_code=500)

    app.include_router(api_user.router)
    app.include_router(api_upload.router)
    app.include_router(api_admin.router)

    def page(name: str) -> Response:
        return FileResponse(
            WEB_DIR / name, media_type="text/html; charset=utf-8", headers={"Content-Security-Policy": _PAGE_CSP}
        )

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        return page("index.html")

    @app.get("/admin", include_in_schema=False)
    def admin_redirect() -> Response:
        return RedirectResponse("/admin/")

    @app.get("/admin/", include_in_schema=False)
    def admin_page() -> Response:
        return page("admin.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    return app
