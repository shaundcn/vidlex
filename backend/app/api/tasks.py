"""任务列表与任务操作（立项 4.3）。按钮能不能点由后端的 allowed_actions 决定（技术选型第 5 节）。"""

import sqlite3
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends

from app.api.deps import get_actions, get_db
from app.api.schemas import TaskOut
from app.services import queries
from app.services.actions import Actions

router = APIRouter(prefix="/api/tasks")


@router.get("", response_model=list[TaskOut])
async def list_tasks(db: Annotated[sqlite3.Connection, Depends(get_db)]) -> list[TaskOut]:
    return [TaskOut(**asdict(t)) for t in queries.task_infos(db)]


@router.post("/{task_id}/{action}", response_model=TaskOut)
async def task_action(
    task_id: int, action: Literal["start", "stop", "ignore"],
    actions: Annotated[Actions, Depends(get_actions)], db: Annotated[sqlite3.Connection, Depends(get_db)],
) -> TaskOut:
    await getattr(actions, action)(task_id)
    return TaskOut(**asdict(queries.task_info(db, task_id)))
