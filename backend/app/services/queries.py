"""接口用的只读查询：任务列表（含可用操作）和日志翻页（技术选型 13.11）。

接口层只调用这里和其他 services，不直接调用 repo 或 domain（技术选型 13.2 的依赖方向）。
"""

import sqlite3
from dataclasses import dataclass

from app.db import repo
from app.domain import naming, status
from app.domain.errors import NotFound
from app.services.memview import MemView
from app.services.tasklog import RETENTION_MS


@dataclass(frozen=True)
class TaskInfo:
    id: int
    original_name: str
    title: str | None
    created_at: int
    updated_at: int  # 任务页“隐藏 30 天前完成的任务”按它判断
    status: str
    step: str | None
    fail_reason: str | None
    notice: str | None
    analysis_mode: str | None  # video / audio，开始处理时写入
    allowed_actions: list[str]


def task_infos(conn: sqlite3.Connection) -> list[TaskInfo]:
    """全部任务，最新加入的在前；不分页，排序和筛选在浏览器里做（12.5）。"""
    with_op = repo.task_ids_with_op(conn)
    tasks = sorted(repo.list_tasks(conn), key=lambda t: (-t["created_at"], -t["id"]))
    return [_info(t, t["id"] in with_op) for t in tasks]


def task_info(conn: sqlite3.Connection, task_id: int) -> TaskInfo:
    task = repo.get_task(conn, task_id)
    if task is None:
        raise NotFound("任务不存在")
    return _info(task, repo.get_op(conn, task_id) is not None)


def logs_page(
    conn: sqlite3.Connection, view: MemView, task_id: int | None, after_id: int | None,
    before_id: int | None, limit: int,
) -> tuple[list[sqlite3.Row], bool]:
    """不带参数或带 before_id：新的在前，第二项表示还有更早的。
    带 after_id：从旧到新，第二项表示还有更新的；没有新日志时只读内存，不查数据库。
    """
    if after_id is not None:
        if after_id >= view.max_log_id:
            return [], False
        rows = repo.list_logs_after(conn, task_id, after_id, limit + 1)
    else:
        rows = repo.list_logs_before(conn, task_id, before_id, limit + 1)
    return rows[:limit], len(rows) > limit


def log_tasks(conn: sqlite3.Connection, now: int) -> list[sqlite3.Row]:
    """30 天内有日志的任务，用于日志筛选。"""
    return repo.list_log_tasks(conn, now - RETENTION_MS)


def _info(task: sqlite3.Row, has_op: bool) -> TaskInfo:
    return TaskInfo(
        id=task["id"], original_name=naming.original_filename(task["orig_rel_path"]), title=task["title"],
        created_at=task["created_at"], updated_at=task["updated_at"], status=task["status"], step=task["step"],
        fail_reason=task["fail_reason"], notice=task["notice"], analysis_mode=task["analysis_mode"],
        allowed_actions=status.allowed_actions(task["status"], task["step"], has_op,
                                               task["current_video_path"] is not None),
    )
