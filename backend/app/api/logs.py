"""任务日志（立项第 5 节）。按“某条之前 / 之后”翻页，不返回总数（技术选型 13.11）。"""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_db, get_memview, get_runtime
from app.api.schemas import LogItem, LogsOut, LogTask
from app.services import queries
from app.services.memview import MemView
from app.services.runtime import Runtime

router = APIRouter(prefix="/api/logs")


@router.get("", response_model=LogsOut)
async def list_logs(
    db: Annotated[sqlite3.Connection, Depends(get_db)], view: Annotated[MemView, Depends(get_memview)],
    task_id: int | None = None, after_id: Annotated[int | None, Query(ge=0)] = None,
    before_id: Annotated[int | None, Query(ge=1)] = None, limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> LogsOut:
    if after_id is not None and before_id is not None:
        raise HTTPException(status_code=422, detail="after_id 和 before_id 不能同时使用")
    rows, has_more = queries.logs_page(db, view, task_id, after_id, before_id, limit)
    return LogsOut(items=[LogItem(**dict(r)) for r in rows], has_more=has_more)


@router.get("/tasks", response_model=list[LogTask])
async def log_tasks(
    db: Annotated[sqlite3.Connection, Depends(get_db)], runtime: Annotated[Runtime, Depends(get_runtime)],
) -> list[LogTask]:
    return [LogTask(task_id=r["task_id"], task_name=r["task_name"])
            for r in queries.log_tasks(db, runtime.clock.now_ms())]
