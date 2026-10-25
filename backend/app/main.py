"""组装应用：启动与关闭、静态文件缓存头、统一错误处理（技术选型 13.2、13.12）。"""

import mimetypes
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import health, logs, queue, settings, tasks, view
from app.config import Config, load_config
from app.domain.errors import Conflict, Degraded, NotFound, VidlexError
from app.logging_setup import set_api_key, setup_logging
from app.services import media, startup
from app.services.ark_client import ArkApi, ArkClient
from app.services.clock import Clock
from app.services.cloud import Cloud
from app.services.fsops import Library
from app.services.actions import Actions
from app.services.journal import Journal
from app.services.memview import MemView
from app.services.runtime import Runtime
from app.services.scanner import Scanner
from app.services.selftest import SelfTest
from app.services.worker import Worker

log = logging.getLogger("vidlex.main")

PORT = 8462
STATIC_DIR = Path(__file__).parent / "static"
# Debian 镜像的 MIME 类型表里没有 woff2（本机有），显式登记，两边返回一致
mimetypes.add_type("font/woff2", ".woff2")
_ERROR_STATUS: dict[type[VidlexError], int] = {NotFound: 404, Conflict: 409, Degraded: 503}


def build_runtime(conn: sqlite3.Connection, config: Config, ark: ArkApi, clock: Clock) -> Runtime:
    library = Library(config.library_dir)
    journal = Journal(conn, library, clock)
    cloud = Cloud(conn, ark, clock)
    worker = Worker(conn, library, journal, cloud, ark, clock, config.data_dir)
    return Runtime(conn, library, journal, Scanner(conn, library, clock), worker, cloud, ark, clock)


def create_app(
    config: Config | None = None, *, ark: ArkApi | None = None, start_background: bool = True
) -> FastAPI:
    """ark、start_background 只给测试用：传入假的方舟、不启动后台循环。"""
    config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ffmpeg = await media.ffmpeg_version()
        clock = Clock()
        db = startup.init_database(config, clock.now_ms())
        app.state.db = db.conn
        app.state.degraded_reason = db.degraded_reason
        app.state.schema_version = db.schema_version
        runtime = None
        client: ArkClient | None = None
        if db.conn is not None:
            # 正式运行时共用一个网络客户端，关闭时释放（13.4）；测试传入假的方舟
            api = ark if ark is not None else (client := ArkClient.create())
            runtime = build_runtime(db.conn, config, api, clock)
            app.state.runtime = runtime
            app.state.memview = MemView(db.conn)
            app.state.actions = Actions(db.conn, runtime.journal, runtime, clock)
            app.state.selftest = SelfTest(db.conn, runtime.ark, runtime.cloud, clock)
            set_api_key(app.state.memview.settings["api_key"])
            error = await startup.init_library(db.conn, runtime.lib, runtime.journal, clock)
            runtime.storage_fault = error is not None and error.storage
            if start_background:
                runtime.start()
        yield
        if runtime is not None:
            await runtime.shutdown()
        if client is not None:
            await client.aclose()
        if app.state.db is not None:
            app.state.db.close()

    app = FastAPI(title="vidlex", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.db = None
    app.state.degraded_reason = "正在启动"
    app.state.schema_version = None
    app.state.ffmpeg = None

    @app.middleware("http")
    async def static_no_cache(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if not request.url.path.startswith(("/api/", "/health")):
            response.headers["Cache-Control"] = "no-cache"  # 静态文件：每次向服务器确认是否有新版本
        return response

    @app.exception_handler(VidlexError)
    async def business_error(request: Request, exc: VidlexError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=_ERROR_STATUS.get(type(exc), 500))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("未处理的异常：%s %s", request.method, request.url.path)
        return JSONResponse({"detail": "服务器内部错误"}, status_code=500)

    for module in (health, queue, tasks, view, settings, logs):
        app.include_router(module.router)
    # 前端页面最后挂载，接口路由优先匹配；“/”打开任务页
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def main() -> None:
    setup_logging()
    uvicorn.run(create_app(), host="0.0.0.0", port=PORT, workers=1, log_config=None)


if __name__ == "__main__":
    main()
