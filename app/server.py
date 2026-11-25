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

from app import __version__
from app.api import deps
from app.api.ws import hub
from app.paths import PathError
from app.runtime import resource_root

logger = logging.getLogger("kwey.server")

# 冻结包内 static/ 随包解压到 _MEIPASS，源码运行即项目根；两种形态统一由 runtime 解析。
STATIC_DIR = resource_root() / "static"

PROJECT_ROOT = resource_root()


def _ok(code: int, data=None, message: str = "ok") -> JSONResponse:
    return JSONResponse({"code": code, "data": data, "message": message})


def create_app(
    initial_vault: Optional[str] = None,
    start_watcher: bool = False,
    desktop: bool = False,
) -> FastAPI:
    """创建 FastAPI 应用。

    Args:
        initial_vault: 启动即打开的 vault 绝对路径（--vault 参数）。
        start_watcher: 是否随 vault 打开启动文件监听（桌面/server 正常运行 True）。
        desktop: 是否桌面原生窗口模式。只有桌面模式存在 pywebview 审批桥；
            纯 --server 模式只读预览，批准 apply 默认关闭。
    """

    deps.set_runtime_mode("desktop" if desktop else "server")

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
                # A0 值班：Timer / worker / EventBus 订阅（启动补跑在首轮 tick）
                deps.start_duty()
                # F2：启动补推 + 排定定时器（内部 daemon 线程，非阻塞）
                start_autopush()
        else:
            hub.bind_eventbus()
        yield
        stop_autopush()   # F2：停掉自动推送定时器，避免退出后残留线程
        deps.stop_duty()  # 停 worker、解绑旧 bus、bump generation
        deps.reset_services()

    app = FastAPI(title="Kwey", version=__version__, lifespan=lifespan)

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
    from app.api.routes_trash import router as trash_router
    from app.api.routes_templates import router as templates_router
    from app.api.routes_export import router as export_router
    from app.api.ws import router as ws_router
    # ---- M2 Agent Link（四卡并行产出，统一在此注册） ----
    from app.api.routes_agent import router as agent_router
    from app.api.routes_organize import router as organize_router
    from app.api.routes_history import router as history_router
    from app.api.routes_provider import router as provider_router
    from app.api.routes_pulse import router as pulse_router
    from app.api.routes_chat import router as chat_router
    # ---- Wave A 并行卡（A4-A8）：编译 / 记忆 / 上下文 ----
    from app.api.routes_compile import router as compile_router
    from app.api.routes_memory import router as memory_router
    from app.api.routes_context import router as context_router
    from app.api.routes_autopush import router as autopush_router
    from app.api.routes_autopush import start_autopush, stop_autopush
    # ---- Wave A 第二批（C1-C6）：Canvas / 周期 / 插件 / 主题 / RAG / 复习 / 剪藏 ----
    from app.api.routes_canvas import router as canvas_router
    from app.api.routes_periodic import router as periodic_router
    from app.api.routes_plugins import router as plugins_router
    from app.api.routes_theme import router as theme_router
    from app.api.routes_rag import router as rag_router
    from app.api.routes_review import router as review_router
    from app.api.routes_clip import router as clip_router
    from app.api.routes_import import router as import_router
    from app.api.routes_views import router as views_router
    from app.api.routes_vector import router as vector_router
    from app.api.routes_update import router as update_router
    from app.api.routes_health import router as health_router
    from app.api.routes_inbox import router as inbox_router
    from app.api.routes_provenance import router as provenance_router
    from app.api.routes_freshness import router as freshness_router
    from app.api.routes_duty import router as duty_router
    from app.api.routes_session import router as session_router

    app.include_router(system_router)
    app.include_router(static_router)
    app.include_router(notes_router)
    app.include_router(files_router)
    app.include_router(search_router)
    app.include_router(graph_router)
    app.include_router(trash_router)
    app.include_router(templates_router)
    app.include_router(export_router)
    app.include_router(ws_router)
    app.include_router(agent_router)
    app.include_router(organize_router)
    app.include_router(history_router)
    app.include_router(provider_router)
    app.include_router(pulse_router)
    app.include_router(chat_router)
    app.include_router(compile_router)
    app.include_router(memory_router)
    app.include_router(context_router)
    app.include_router(autopush_router)   # F2 每日自动推送
    app.include_router(canvas_router)
    app.include_router(periodic_router)
    app.include_router(plugins_router)
    app.include_router(theme_router)
    app.include_router(rag_router)
    app.include_router(review_router)
    app.include_router(clip_router)
    app.include_router(import_router)   # P0 库导入（Obsidian / Markdown）
    app.include_router(views_router)    # Bases 式结构化视图
    app.include_router(vector_router)   # 语义检索（模型下载/重建/试查）
    app.include_router(update_router)   # 版本检查（告知式更新）
    app.include_router(health_router)   # 知识体检
    app.include_router(inbox_router)    # 快速捕获 + AI 收件箱
    app.include_router(provenance_router)  # 写入溯源（读 / 人工审阅）
    app.include_router(freshness_router)   # P2-D 知识保鲜（只读状态 / 替代路径）
    app.include_router(duty_router)        # A0 值班（职责 / 提案 / 审批 / 暂停）
    app.include_router(session_router)     # C 会话蒸馏草案（预览 / 确认落地）

    # ---------------------------------------------------------- 静态托管
    @app.middleware("http")
    async def _no_cache_static(request: Request, call_next):
        """本地软件随版本更新 JS/CSS：静态资源禁强缓存，避免浏览器显示旧界面。"""
        response = await call_next(request)
        if request.url.path.startswith("/static/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"), media_type="text/html; charset=utf-8")

    return app
