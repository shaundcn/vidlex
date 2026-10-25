"""全部 SQL 都在这里，全部参数化（技术选型 4.11、5）。每个写事务一个函数（4.7）。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.domain.audio import AnalysisMode  # 只用于类型标注（技术选型 13.2 依赖方向）
from app.domain.status import OpKind, Step


@contextmanager
def _write(conn: sqlite3.Connection, tasks: bool = True) -> Iterator[None]:
    """写事务。tasks=False 表示没有改动任务列表（日志、设置、队列状态、云端副本），列表版本号不变。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    for listener in getattr(conn, "commit_listeners", ()):
        listener(tasks)


def ensure_singletons(conn: sqlite3.Connection, prompts: dict[str, str], now: int) -> None:
    """补齐 settings、runtime_state 两张单行表；已有的行不动。"""
    with _write(conn, tasks=False):
        conn.execute(
            "INSERT OR IGNORE INTO settings (id, prompt_title, prompt_main_content,"
            " prompt_highlight, prompt_transcript, updated_at) VALUES (1, ?, ?, ?, ?, ?)",
            (prompts["title"], prompts["main_content"], prompts["highlight"],
             prompts["transcript"], now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO runtime_state (id, updated_at) VALUES (1, ?)", (now,)
        )


def get_settings(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()


def get_runtime_state(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone()


def enqueue_new_task(
    conn: sqlite3.Connection,
    orig_rel_path: str,
    current_video_path: str,
    ident: tuple[int, int, int, int],
    now: int,
) -> int:
    """新视频入队：插入任务，排到队尾。ident 为 (dev, ino, size, mtime_ns)。"""
    with _write(conn):
        cur = conn.execute(
            "INSERT INTO tasks (orig_rel_path, current_video_path, status, queue_seq,"
            " src_dev, src_ino, src_size, src_mtime_ns, created_at, updated_at)"
            " VALUES (?, ?, 'queued',"
            " (SELECT COALESCE(MAX(queue_seq), 0) + 1 FROM tasks WHERE status = 'queued'),"
            " ?, ?, ?, ?, ?, ?)",
            (orig_rel_path, current_video_path, *ident, now, now),
        )
    assert cur.lastrowid is not None  # INSERT 成功后总有值
    return cur.lastrowid


def list_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tasks").fetchall()


def add_log(
    conn: sqlite3.Connection, task_id: int, task_name: str, ts: int, level: str, message: str
) -> int:
    with _write(conn, tasks=False):
        cur = conn.execute(
            "INSERT INTO task_logs (task_id, task_name, ts, level, message)"
            " VALUES (?, ?, ?, ?, ?)",
            (task_id, task_name, ts, level, message),
        )
    assert cur.lastrowid is not None  # INSERT 成功后总有值
    return cur.lastrowid


def list_logs_before(
    conn: sqlite3.Connection, task_id: int | None, before_id: int | None, limit: int
) -> list[sqlite3.Row]:
    """取比 before_id 更早的一页日志（新的在前）；before_id 为空时取最新的一页。"""
    # 不写成 “? IS NULL OR …”：那样 SQLite 用不上 task_logs(task_id, id) 索引
    before = before_id if before_id is not None else 2**63 - 1  # SQLite 整数上限
    if task_id is None:
        return conn.execute(
            "SELECT * FROM task_logs WHERE id < ? ORDER BY id DESC LIMIT ?",
            (before, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM task_logs WHERE task_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
        (task_id, before, limit),
    ).fetchall()


# ---- 查询 ----

def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def get_op(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM task_ops WHERE task_id = ?", (task_id,)).fetchone()


def list_ops(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM task_ops ORDER BY task_id").fetchall()


def list_interrupted(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """处理中、没有进度记录的任务（重启前还没到保存结果）。"""
    return conn.execute(
        "SELECT * FROM tasks WHERE status = 'processing'"
        " AND id NOT IN (SELECT task_id FROM task_ops)"
    ).fetchall()


def row_ident(row: sqlite3.Row) -> tuple[int, int, int, int]:
    """任务或进度记录里保存的文件身份：(设备号, inode, 大小, 修改时间纳秒)。"""
    return (row["src_dev"], row["src_ino"], row["src_size"], row["src_mtime_ns"])


def path_taken(conn: sqlite3.Connection, rel: str) -> bool:
    """路径是否已被任务或进度记录的目标占用（技术选型 4.11）。"""
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM tasks WHERE current_video_path = ?1 OR md_path = ?1)"
        " OR EXISTS (SELECT 1 FROM task_ops WHERE video_target_path = ?1 OR md_target_path = ?1)",
        (rel,),
    ).fetchone()[0] == 1


# ---- 进度记录（技术选型 4.7）----

_TAIL = "(SELECT COALESCE(MAX(queue_seq), 0) + 1 FROM tasks WHERE status = 'queued')"
_HEAD = "(SELECT COALESCE(MIN(queue_seq), 1) - 1 FROM tasks WHERE status = 'queued')"
# 完成 redo / ignore / unignore 时，任务的身份快照取进度记录里的（点击那一刻的文件）
_SNAPSHOT_FROM_OP = (
    "src_dev = o.src_dev, src_ino = o.src_ino, src_size = o.src_size, src_mtime_ns = o.src_mtime_ns"
)


def begin_commit(
    conn: sqlite3.Connection,
    task_id: int,
    src_path: str,
    ident: tuple[int, int, int, int],
    video_target_path: str,
    md_tmp_path: str,
    md_target_path: str,
    title: str,
    now: int,
) -> None:
    """进入保存结果：写 commit 记录，同一事务里步骤改为 commit。"""
    with _write(conn):
        conn.execute(
            "INSERT INTO task_ops (task_id, kind, phase, src_path, src_dev, src_ino, src_size,"
            " src_mtime_ns, video_target_path, md_tmp_path, md_target_path, title, created_at, updated_at)"
            " VALUES (?, 'commit', 'recorded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, src_path, *ident, video_target_path, md_tmp_path, md_target_path, title, now, now),
        )
        conn.execute(
            "UPDATE tasks SET step = 'commit', updated_at = ? WHERE id = ? AND status = 'processing'",
            (now, task_id),
        )


def begin_op(
    conn: sqlite3.Connection,
    task_id: int,
    kind: OpKind,
    src_path: str,
    ident: tuple[int, int, int, int],
    video_target_path: str,
    old_md_path: str | None,
    now: int,
) -> None:
    """写 redo / ignore / unignore 记录。"""
    with _write(conn):
        conn.execute(
            "INSERT INTO task_ops (task_id, kind, phase, src_path, src_dev, src_ino, src_size,"
            " src_mtime_ns, video_target_path, old_md_path, created_at, updated_at)"
            " VALUES (?, ?, 'recorded', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, kind, src_path, *ident, video_target_path, old_md_path, now, now),
        )


def advance_op(
    conn: sqlite3.Connection, task_id: int, phase: str, video_path: str | None, now: int
) -> None:
    """更新步骤；移动过视频时，同一事务里更新 current_video_path。"""
    with _write(conn):
        conn.execute(
            "UPDATE task_ops SET phase = ?, updated_at = ? WHERE task_id = ?", (phase, now, task_id)
        )
        if video_path is not None:
            conn.execute(
                "UPDATE tasks SET current_video_path = ?, updated_at = ? WHERE id = ?",
                (video_path, now, task_id),
            )


def finish_commit(
    conn: sqlite3.Connection, task_id: int, title: str, md_path: str, video_path: str | None, now: int
) -> None:
    """处理完成：写入标题和 md 路径，改为已完成，删除记录，连续失败数清零，清空操作提示。"""
    with _write(conn):
        conn.execute(
            "UPDATE tasks SET status = 'completed', step = NULL, queue_seq = NULL, fail_reason = NULL,"
            " notice = NULL, title = ?, md_path = ?, current_video_path = ?, updated_at = ?"
            " WHERE id = ?",
            (title, md_path, video_path, now, task_id),
        )
        conn.execute("DELETE FROM task_ops WHERE task_id = ?", (task_id,))
        conn.execute("UPDATE runtime_state SET consecutive_failures = 0, updated_at = ? WHERE id = 1", (now,))


def finish_redo(conn: sqlite3.Connection, task_id: int, now: int) -> None:
    """重新处理准备完成：排到队尾，清空标题、md 路径、尝试次数和操作提示。"""
    with _write(conn):
        conn.execute(
            f"UPDATE tasks SET status = 'queued', queue_seq = {_TAIL}, title = NULL, md_path = NULL,"
            f" attempts = 0, notice = NULL, updated_at = ?, {_SNAPSHOT_FROM_OP}"
            " FROM task_ops AS o WHERE o.task_id = tasks.id AND tasks.id = ?",
            (now, task_id),
        )
        conn.execute("DELETE FROM task_ops WHERE task_id = ?", (task_id,))


def finish_ignore(conn: sqlite3.Connection, task_id: int, now: int) -> None:
    with _write(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ignored', queue_seq = NULL, fail_reason = NULL, notice = NULL,"
            f" updated_at = ?, {_SNAPSHOT_FROM_OP}"
            " FROM task_ops AS o WHERE o.task_id = tasks.id AND tasks.id = ?",
            (now, task_id),
        )
        conn.execute("DELETE FROM task_ops WHERE task_id = ?", (task_id,))


def finish_unignore(conn: sqlite3.Connection, task_id: int, now: int) -> None:
    """取消忽略完成：排到队尾，尝试次数清零。"""
    with _write(conn):
        conn.execute(
            f"UPDATE tasks SET status = 'queued', queue_seq = {_TAIL}, attempts = 0, notice = NULL,"
            f" updated_at = ?, {_SNAPSHOT_FROM_OP}"
            " FROM task_ops AS o WHERE o.task_id = tasks.id AND tasks.id = ?",
            (now, task_id),
        )
        conn.execute("DELETE FROM task_ops WHERE task_id = ?", (task_id,))


def abort_op(conn: sqlite3.Connection, task_id: int, notice: str, now: int) -> None:
    """剪切失败：删除记录，任务保持原状态，写入操作提示。"""
    with _write(conn):
        conn.execute("DELETE FROM task_ops WHERE task_id = ?", (task_id,))
        conn.execute("UPDATE tasks SET notice = ?, updated_at = ? WHERE id = ?", (notice, now, task_id))


def set_notice(conn: sqlite3.Connection, task_id: int, notice: str, now: int) -> None:
    with _write(conn):
        conn.execute("UPDATE tasks SET notice = ?, updated_at = ? WHERE id = ?", (notice, now, task_id))


def remove_task(conn: sqlite3.Connection, task_id: int) -> None:
    """视频消失或被替换：删除进度记录和任务。"""
    with _write(conn):
        conn.execute("DELETE FROM task_ops WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


# ---- 启动 ----

def requeue_front(
    conn: sqlite3.Connection,
    task_id: int,
    attempts: int,
    now: int,
    queue: tuple[str, str | None, bool] | None = None,
) -> None:
    """退回排队最前（重启中断、影响所有任务的错误）；queue 不为空时同一事务里改队列状态。"""
    with _write(conn):
        conn.execute(
            f"UPDATE tasks SET status = 'queued', step = NULL, queue_seq = {_HEAD}, attempts = ?,"
            " updated_at = ? WHERE id = ?",
            (attempts, now, task_id),
        )
        if queue is not None:
            _set_queue(conn, *queue, now)


def mark_failed(
    conn: sqlite3.Connection,
    task_id: int,
    reason: str,
    attempts: int,
    failures: int,
    queue: tuple[str, str | None, bool] | None,
    now: int,
) -> None:
    """处理失败：改为失败，更新连续失败数；queue 不为空时同一事务里改队列状态（出错）。"""
    with _write(conn):
        conn.execute(
            "UPDATE tasks SET status = 'failed', step = NULL, queue_seq = NULL, fail_reason = ?,"
            " attempts = ?, updated_at = ? WHERE id = ?",
            (reason, attempts, now, task_id),
        )
        conn.execute(
            "UPDATE runtime_state SET consecutive_failures = ?, updated_at = ? WHERE id = 1",
            (failures, now),
        )
        if queue is not None:
            _set_queue(conn, *queue, now)


def set_queue_state(
    conn: sqlite3.Connection,
    state: str,
    error_message: str | None,
    auto_resume: bool,
    reset_failures: bool,
    now: int,
) -> None:
    with _write(conn, tasks=False):
        _set_queue(conn, state, error_message, auto_resume, now)
        if reset_failures:
            conn.execute("UPDATE runtime_state SET consecutive_failures = 0 WHERE id = 1")


def set_library_id(conn: sqlite3.Connection, library_id: str, now: int) -> None:
    with _write(conn, tasks=False):
        conn.execute(
            "UPDATE runtime_state SET library_id = ?, updated_at = ? WHERE id = 1", (library_id, now)
        )


def _set_queue(
    conn: sqlite3.Connection, state: str, error_message: str | None, auto_resume: bool, now: int
) -> None:
    conn.execute(
        "UPDATE runtime_state SET queue_state = ?, error_message = ?, error_auto_resume = ?,"
        " updated_at = ? WHERE id = 1",
        (state, error_message, int(auto_resume), now),
    )


# ---- 处理流程（技术选型 4.7）----

def next_queued(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tasks WHERE status = 'queued' ORDER BY queue_seq, id LIMIT 1"
    ).fetchone()


def start_processing(
    conn: sqlite3.Connection, task_id: int, step: Step, ident: tuple[int, int, int, int], mode: AnalysisMode, now: int
) -> bool:
    """开始处理：状态改为处理中、写入步骤和分析方式、更新身份快照。任务已不是排队中时不改，返回 False。"""
    with _write(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'processing', step = ?, queue_seq = NULL, notice = NULL, analysis_mode = ?,"
            " src_dev = ?, src_ino = ?, src_size = ?, src_mtime_ns = ?, updated_at = ?"
            " WHERE id = ? AND status = 'queued'",
            (step, mode, *ident, now, task_id),
        )
    return cur.rowcount == 1


def set_step(conn: sqlite3.Connection, task_id: int, step: Step, now: int) -> None:
    with _write(conn):
        conn.execute(
            "UPDATE tasks SET step = ?, updated_at = ? WHERE id = ? AND status = 'processing'",
            (step, now, task_id),
        )


def set_attempts(conn: sqlite3.Connection, task_id: int, attempts: int, now: int) -> None:
    with _write(conn):
        conn.execute("UPDATE tasks SET attempts = ?, updated_at = ? WHERE id = ?", (attempts, now, task_id))


def mark_stopped(conn: sqlite3.Connection, task_id: int, now: int) -> None:
    with _write(conn):
        conn.execute(
            "UPDATE tasks SET status = 'stopped', step = NULL, queue_seq = NULL, notice = NULL,"
            " updated_at = ? WHERE id = ? AND status IN ('queued', 'processing')",
            (now, task_id),
        )


def add_cloud_file(conn: sqlite3.Connection, file_id: str, expire_at: int, now: int) -> None:
    with _write(conn, tasks=False):
        conn.execute(
            "INSERT OR REPLACE INTO cloud_files (file_id, expire_at, created_at) VALUES (?, ?, ?)",
            (file_id, expire_at, now),
        )


def remove_cloud_file(conn: sqlite3.Connection, file_id: str) -> None:
    with _write(conn, tasks=False):
        conn.execute("DELETE FROM cloud_files WHERE file_id = ?", (file_id,))


def list_cloud_files(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM cloud_files ORDER BY created_at").fetchall()


def delete_logs_before(conn: sqlite3.Connection, ts: int) -> None:
    with _write(conn, tasks=False):
        conn.execute("DELETE FROM task_logs WHERE ts < ?", (ts,))


# ---- 接口（阶段 3c）----

def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    return {row[0]: row[1] for row in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")}


def max_log_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_logs").fetchone()[0]


def task_ids_with_op(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT task_id FROM task_ops")}


def list_logs_after(
    conn: sqlite3.Connection, task_id: int | None, after_id: int, limit: int
) -> list[sqlite3.Row]:
    """比 after_id 新的日志，从旧到新。"""
    if task_id is None:
        return conn.execute(
            "SELECT * FROM task_logs WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM task_logs WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
        (task_id, after_id, limit),
    ).fetchall()


def list_log_tasks(conn: sqlite3.Connection, since_ts: int) -> list[sqlite3.Row]:
    """since_ts 之后有日志的任务；名称取该任务最新一条日志里的。"""
    return conn.execute(
        "SELECT task_id, task_name FROM task_logs WHERE id IN"
        " (SELECT MAX(id) FROM task_logs WHERE ts >= ? GROUP BY task_id) ORDER BY id DESC",
        (since_ts,),
    ).fetchall()


def requeue_tail(conn: sqlite3.Connection, task_id: int, now: int) -> None:
    """失败、已停止的任务点“开始”：排到队尾，清空失败原因和操作提示，尝试次数清零。"""
    with _write(conn):
        conn.execute(
            f"UPDATE tasks SET status = 'queued', queue_seq = {_TAIL}, fail_reason = NULL, notice = NULL,"
            " attempts = 0, updated_at = ? WHERE id = ? AND status IN ('failed', 'stopped')",
            (now, task_id),
        )


def save_api_key(conn: sqlite3.Connection, key: str, now: int) -> None:
    with _write(conn, tasks=False):
        conn.execute("UPDATE settings SET api_key = ?, updated_at = ? WHERE id = 1", (key, now))


def prompts_of(settings: sqlite3.Row) -> dict[str, str]:
    """settings 行里的 4 条提示词，键是返回字段名（和 save_prompts 对应）。"""
    return {"title": settings["prompt_title"], "main_content": settings["prompt_main_content"],
            "highlight": settings["prompt_highlight"], "transcript": settings["prompt_transcript"]}


def save_prompts(conn: sqlite3.Connection, prompts: dict[str, str], now: int) -> None:
    with _write(conn, tasks=False):
        conn.execute(
            "UPDATE settings SET prompt_title = ?, prompt_main_content = ?, prompt_highlight = ?,"
            " prompt_transcript = ?, updated_at = ? WHERE id = 1",
            (prompts["title"], prompts["main_content"], prompts["highlight"], prompts["transcript"], now),
        )


def save_sleep(conn: sqlite3.Connection, enabled: bool, idle_minutes: int, interval_hours: int, now: int) -> None:
    with _write(conn, tasks=False):
        conn.execute(
            "UPDATE settings SET sleep_enabled = ?, sleep_idle_minutes = ?, sleep_interval_hours = ?,"
            " updated_at = ? WHERE id = 1",
            (int(enabled), idle_minutes, interval_hours, now),
        )
