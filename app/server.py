"""FastAPI 应用工厂：路由注册、统一响应包装、静态托管、CORS、生命周期。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import deps
from app.api.ws import hub
from app.paths import PathError

logger = logging.getLogger("kwey.server")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ok(code: int, data=None, message: str = "ok") -> JSONResponse:
    return JSONResponse({"code": code, "data": data, "message": message})


def create_app(initial_vault: Optional[str] = None, start_watcher: bool = False) -> FastAPI:
    """创建 FastAPI 应用。

    Args:
        initial_vault: 启动即打开的 vault 绝对路径（--vault 参数）。
        start_watcher: 是否随 vault 打开启动文件监听（桌面/server 正常运行 True）。
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # WS 出口订阅 EventBus（EventBus 实例随 vault 创建/重建，故在 init 后订阅）
        hub.setup()
        if initial_vault:
            root = Path(initial_vault).resolve()
            if not root.is_dir():
                logger.warning("指定的 vault 不存在: %s", root)
            else:
                deps.init_services(root, open_now=True)
                hub.bind_eventbus()
                if start_watcher:
                    deps.get_vault().start_watcher()
        else:
            hub.bind_eventbus()
        yield
        deps.reset_services()

    app = FastAPI(title="Kwey", version="0.1.0", lifespan=lifespan)

    # ---------------------------------------------------------- 统一响应包装
    @app.exception_handler(StarletteHTTPException)
    async def http_exc_handler(_request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            {"code": exc.status_code, "data": None, "message": str(exc.detail)},
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            {"code": 400, "data": None, "message": f"参数错误: {exc.errors()[:3]}"},
            status_code=400,
        )

    @app.exception_handler(PathError)
    async def path_error_handler(_request: Request, exc: PathError):
        return JSONResponse({"code": 400, "data": None, "message": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def generic_handler(_request: Request, exc: Exception):
        logger.exception("未处理异常")
        return JSONResponse(
            {"code": 500, "data": None, "message": f"内部错误: {exc}"},
            status_code=500,
        )

    # ---------------------------------------------------------- 路由注册
    from app.api.routes_system import router as system_router
    from app.api.routes_system import static_router
    from app.api.routes_notes import router as notes_router
    from app.api.routes_files import router as files_router
    from app.api.routes_search import router as search_router
    from app.api.routes_graph import router as graph_router
    from app.api.ws import router as ws_router

    app.include_router(system_router)
    app.include_router(static_router)
    app.include_router(notes_router)
    app.include_router(files_router)
    app.include_router(search_router)
    app.include_router(graph_router)
    app.include_router(ws_router)

    # ---------------------------------------------------------- 静态托管
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"), media_type="text/html; charset=utf-8")

    return app
