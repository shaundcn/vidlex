"""接口测试：用 httpx2.ASGITransport 在同一个事件循环里直接调用应用（技术选型 13.3）。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI

from app.config import Config
from app.main import create_app


@asynccontextmanager
async def running(config: Config, ark=None, start_background: bool = False,
                  app: FastAPI | None = None) -> AsyncIterator[tuple[FastAPI, httpx2.AsyncClient]]:
    app = app or create_app(config, ark=ark, start_background=start_background)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield app, client
