"""任务日志（立项第 5 节）：写入时过滤 Key，保留 30 天。"""

import logging
import sqlite3

from app.db import repo
from app.domain import naming
from app.logging_setup import mask

log = logging.getLogger("vidlex.task")
_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


def write(
    conn: sqlite3.Connection, now: int, task_id: int, task_name: str, level: str, message: str
) -> None:
    """写进数据库前过滤 API Key；同时输出到程序日志。"""
    message = mask(message)
    repo.add_log(conn, task_id, task_name, now, level, message)
    log.log(_LEVELS[level], "任务 %d %s：%s", task_id, task_name, message)


def write_task(conn: sqlite3.Connection, now: int, task_id: int, level: str, message: str) -> None:
    """按任务现在的显示名写日志（完成前是原文件名，完成后是新标题）；任务已删除时用编号。"""
    task = repo.get_task(conn, task_id)
    name = naming.display_name(task["orig_rel_path"], task["title"]) if task else str(task_id)
    write(conn, now, task_id, name, level, message)


RETENTION_MS = 30 * 24 * 3600 * 1000


def cleanup(conn: sqlite3.Connection, now: int) -> None:
    """删除 30 天前的日志。"""
    repo.delete_logs_before(conn, now - RETENTION_MS)
