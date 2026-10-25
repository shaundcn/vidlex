"""取服务对象的依赖函数。降级模式下抛 Degraded，由统一错误处理转成 503。"""

import sqlite3

from fastapi import Request

from app.domain.errors import Degraded
from app.services.actions import Actions
from app.services.fsops import Library
from app.services.memview import MemView
from app.services.runtime import Runtime
from app.services.selftest import SelfTest


def _ready(request: Request) -> None:
    if request.app.state.db is None:
        raise Degraded(request.app.state.degraded_reason)


async def get_db(request: Request) -> sqlite3.Connection:
    _ready(request)
    return request.app.state.db


async def get_actions(request: Request) -> Actions:
    _ready(request)
    return request.app.state.actions


async def get_memview(request: Request) -> MemView:
    _ready(request)
    return request.app.state.memview


async def get_runtime(request: Request) -> Runtime:
    _ready(request)
    return request.app.state.runtime


async def get_library(request: Request) -> Library:
    _ready(request)
    return request.app.state.runtime.lib


async def get_selftest(request: Request) -> SelfTest:
    _ready(request)
    return request.app.state.selftest
