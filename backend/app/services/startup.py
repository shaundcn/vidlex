"""启动顺序（技术选型 13.4）：第 1 步数据库和降级模式，第 2–5 步视频库和恢复。"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

from app.config import Config
from app.db import connection, repo
from app.domain import status
from app.domain.errors import MAX_ATTEMPTS, GlobalError, classify_os_error
from app.domain.prompt import DEFAULT_PROMPTS
from app.services import tasklog
from app.services.clock import Clock
from app.services.fsops import Library
from app.services.journal import Journal

INTERRUPTED_TOO_OFTEN = "处理多次中断"

log = logging.getLogger("vidlex.startup")


@dataclass
class DatabaseState:
    conn: sqlite3.Connection | None
    degraded_reason: str | None
    schema_version: int | None


def init_database(config: Config, now: int) -> DatabaseState:
    """打开数据库 → quick_check → 版本检查 → 迁移 → 补齐单行表。

    任何一步失败都进入降级模式：不启动后台循环，不碰视频库。
    """
    try:
        conn = connection.open_database(config.db_path)
    except connection.DatabaseCorrupt as exc:
        return _degraded("数据库文件损坏", exc)
    except connection.DatabaseTooNew as exc:
        return _degraded("数据库版本比程序新，请升级 vidlex", exc)
    except connection.MigrationFailed as exc:
        return _degraded("数据库升级失败", exc)
    except (OSError, sqlite3.Error) as exc:
        return _degraded("data 文件夹无法读写", exc)
    try:
        repo.ensure_singletons(conn, DEFAULT_PROMPTS, now)
    except sqlite3.Error as exc:
        conn.close()
        return _degraded("数据库文件损坏", exc)
    return DatabaseState(conn, None, connection.schema_version(conn))


def _degraded(reason: str, exc: Exception) -> DatabaseState:
    log.error("进入降级模式：%s（%s）", reason, exc)
    return DatabaseState(None, reason, None)


async def init_library(
    conn: sqlite3.Connection, library: Library, journal: Journal, clock: Clock
) -> GlobalError | None:
    """启动顺序第 2–5 步；存储故障修好后用户点“继续”时也调用。失败时返回错误（队列已进入出错）。

    第 2 步不通过时不修改任何任务和文件，队列进入出错。
    """
    try:
        state = repo.get_runtime_state(conn)
        library_id = await asyncio.to_thread(library.check, state["library_id"])
        if library_id != state["library_id"]:
            repo.set_library_id(conn, library_id, clock.now_ms())
        await asyncio.to_thread(library.ensure_layout)
        await journal.recover_all()
    except GlobalError as exc:
        _enter_error(conn, clock, exc)
        return exc
    except OSError as exc:
        error = classify_os_error(exc)
        _enter_error(conn, clock, error)
        return error
    _requeue_interrupted(conn, clock)
    keep = {str(op["task_id"]) for op in repo.list_ops(conn) if op["kind"] == "commit"}
    await asyncio.to_thread(library.clean_tmp, keep)
    return None


def _enter_error(conn: sqlite3.Connection, clock: Clock, exc: GlobalError) -> None:
    state = repo.get_runtime_state(conn)
    change = status.on_global_error(
        state["queue_state"], bool(state["error_auto_resume"]), str(exc), exc.auto_resume
    )
    repo.set_queue_state(conn, change.state, change.error_message, change.auto_resume, False, clock.now_ms())
    log.error("队列进入出错：%s", exc)


def _requeue_interrupted(conn: sqlite3.Connection, clock: Clock) -> None:
    """重启前正在处理、还没到保存结果的任务：退回排队最前，尝试次数加 1（立项 8.2）。

    加 1 后达到上限时直接标为失败，避免某个视频每次都让软件崩溃而无限循环。
    """
    for task in repo.list_interrupted(conn):
        now = clock.now_ms()
        attempts = task["attempts"] + 1
        if attempts < MAX_ATTEMPTS:
            repo.requeue_front(conn, task["id"], attempts, now)
            tasklog.write_task(conn, now, task["id"], "warning", f"处理中断，重新排队（第 {attempts} 次）")
            continue
        state = repo.get_runtime_state(conn)
        failures, change = status.after_task_result(
            True, state["consecutive_failures"], state["queue_state"], bool(state["error_auto_resume"])
        )
        queue = (change.state, change.error_message, change.auto_resume) if change else None
        repo.mark_failed(conn, task["id"], INTERRUPTED_TOO_OFTEN, attempts, failures, queue, now)
        tasklog.write_task(conn, now, task["id"], "error", INTERRUPTED_TOO_OFTEN)
