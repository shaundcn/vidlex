"""数据库验收（技术选型 4.13）。约束的负面测试直接写 SQL，绕过 repo，测的是数据库本身。"""

import os
import sqlite3
import stat
import statistics
import time
from pathlib import Path

import pytest

from app.db import connection, repo

IDENT = (1, 100, 2048, 1_700_000_000_000_000_000)


def insert_task(db: sqlite3.Connection, **overrides: object) -> int:
    row: dict[str, object] = {
        "orig_rel_path": "教程/a&抖音&b.mp4", "current_video_path": "input/教程/a&抖音&b.mp4",
        "status": "queued", "queue_seq": 1, "src_dev": 1, "src_ino": 100, "src_size": 2048,
        "src_mtime_ns": 1, "created_at": 1, "updated_at": 1,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    return db.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", tuple(row.values())).lastrowid


def insert_op(db: sqlite3.Connection, task_id: int, **overrides: object) -> None:
    row: dict[str, object] = {
        "task_id": task_id, "kind": "ignore", "phase": "recorded", "src_path": "input/a.mp4",
        "src_dev": 1, "src_ino": 1, "src_size": 1, "src_mtime_ns": 1,
        "video_target_path": "output/ignored/a.mp4", "created_at": 1, "updated_at": 1,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    db.execute(f"INSERT INTO task_ops ({cols}) VALUES ({marks})", tuple(row.values()))


# ---- 能连接、能建表、迁移可重复执行、版本号为最新（2）、能插入并查询 ----

def test_tables_created_and_latest_version(db: sqlite3.Connection) -> None:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"tasks", "task_ops", "task_logs", "cloud_files", "settings", "runtime_state"} <= tables
    assert connection.schema_version(db) == 2
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_reopen_runs_migrations_again_without_error(tmp_path: Path) -> None:
    path = tmp_path / "vidlex.db"
    connection.open_database(path).close()
    conn = connection.open_database(path)
    assert connection.schema_version(conn) == 2
    conn.close()


def test_insert_and_query(db: sqlite3.Connection) -> None:
    first = repo.enqueue_new_task(db, "a.mp4", "input/a.mp4", IDENT, 10)
    second = repo.enqueue_new_task(db, "b.mp4", "input/b.mp4", (1, 101, 1, 1), 11)
    rows = {r["id"]: r for r in repo.list_tasks(db)}
    assert rows[first]["status"] == "queued" and rows[first]["queue_seq"] == 1
    assert rows[second]["queue_seq"] == 2
    log_id = repo.add_log(db, first, "a.mp4", 12, "info", "开始处理")
    assert [r["id"] for r in repo.list_logs_before(db, first, None, 10)] == [log_id]
    assert repo.get_settings(db)["sleep_idle_minutes"] == 30
    assert repo.get_runtime_state(db)["queue_state"] == "running"


def test_singletons_not_overwritten(db: sqlite3.Connection) -> None:
    db.execute("UPDATE settings SET prompt_title = '自定义' WHERE id = 1")
    repo.ensure_singletons(db, {"title": "x", "main_content": "x", "highlight": "x", "transcript": "x"}, 2)
    assert repo.get_settings(db)["prompt_title"] == "自定义"


# ---- 约束的负面测试：都必须被数据库拒绝 ----

reject = pytest.raises(sqlite3.IntegrityError)


def test_reject_unknown_status(db: sqlite3.Connection) -> None:
    with reject:
        insert_task(db, status="deleted")


def test_reject_processing_without_step(db: sqlite3.Connection) -> None:
    with reject:
        insert_task(db, status="processing", queue_seq=None)


def test_reject_failed_without_reason(db: sqlite3.Connection) -> None:
    with reject:
        insert_task(db, status="failed")


def test_reject_completed_without_title(db: sqlite3.Connection) -> None:
    with reject:
        insert_task(db, status="completed", md_path="output/a.md")


def test_reject_two_processing(db: sqlite3.Connection) -> None:
    insert_task(db, status="processing", step="upload")
    with reject:
        insert_task(db, status="processing", step="analyze", current_video_path="input/b.mp4")


def test_reject_same_video_path(db: sqlite3.Connection) -> None:
    insert_task(db)
    with reject:
        insert_task(db)


def test_reject_update_immutable_fields(db: sqlite3.Connection) -> None:
    task_id = insert_task(db)
    with reject:
        db.execute("UPDATE tasks SET orig_rel_path = 'x.mp4' WHERE id = ?", (task_id,))
    with reject:
        db.execute("UPDATE tasks SET created_at = 2 WHERE id = ?", (task_id,))
    db.execute("UPDATE tasks SET updated_at = 2 WHERE id = ?", (task_id,))  # 其他字段可以改


@pytest.mark.parametrize(
    "column, value",
    [("sleep_idle_minutes", 4), ("sleep_interval_hours", 25),
     ("prompt_title", "   "), ("prompt_highlight", "　\n"), ("api_key", " ")],
)
def test_reject_bad_settings(db: sqlite3.Connection, column: str, value: object) -> None:
    with reject:
        db.execute(f"UPDATE settings SET {column} = ? WHERE id = 1", (value,))


def test_reject_second_settings_row(db: sqlite3.Connection) -> None:
    with reject:
        db.execute("INSERT INTO settings (id, prompt_title, prompt_main_content, prompt_highlight,"
                   " prompt_transcript, updated_at) VALUES (2, 'a', 'b', 'c', 'd', 1)")


@pytest.mark.parametrize(
    "overrides",
    [{"kind": "ignore", "phase": "md_placed"},
     {"kind": "unignore", "phase": "video_moved"},
     {"kind": "commit", "phase": "recorded"},  # 缺 md_tmp_path 等
     {"kind": "ignore", "phase": "recorded", "title": "x"},
     {"kind": "ignore", "phase": "recorded", "old_md_path": "output/a.md"}],
)
def test_reject_bad_task_op(db: sqlite3.Connection, overrides: dict[str, object]) -> None:
    task_id = insert_task(db)
    with reject:
        insert_op(db, task_id, **overrides)


def test_accept_valid_task_ops(db: sqlite3.Connection) -> None:
    a = insert_task(db, status="processing", step="commit", queue_seq=None)
    insert_op(db, a, kind="commit", phase="md_placed", md_tmp_path="t", md_target_path="m", title="t")
    b = insert_task(db, current_video_path="input/b.mp4")
    insert_op(db, b, kind="redo", phase="old_md_deleted", old_md_path="output/b.md")


def test_reject_delete_task_with_op(db: sqlite3.Connection) -> None:
    task_id = insert_task(db)
    insert_op(db, task_id)
    with reject:
        db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def test_reject_text_in_integer_column(db: sqlite3.Connection) -> None:
    with reject:
        insert_task(db, src_size="abc")


def test_reject_bad_runtime_state(db: sqlite3.Connection) -> None:
    with reject:
        db.execute("UPDATE runtime_state SET queue_state = 'error' WHERE id = 1")  # 缺原因
    with reject:
        db.execute("UPDATE runtime_state SET error_auto_resume = 1 WHERE id = 1")  # 不是出错


# ---- 权限 ----

def test_db_files_are_0600(db: sqlite3.Connection, tmp_path: Path) -> None:
    repo.add_log(db, 1, "a", 1, "info", "写一条，确保 -wal 存在")
    for name in ("vidlex.db", "vidlex.db-wal", "vidlex.db-shm"):
        mode = stat.S_IMODE(os.stat(tmp_path / name).st_mode)
        assert mode == 0o600, (name, oct(mode))


# ---- 性能抽查：5000 个任务、10 万条日志 ----

def test_performance(db: sqlite3.Connection) -> None:
    db.execute("BEGIN")
    db.executemany(
        "INSERT INTO tasks (orig_rel_path, current_video_path, status, queue_seq, src_dev, src_ino,"
        " src_size, src_mtime_ns, created_at, updated_at) VALUES (?, ?, 'queued', ?, 1, ?, 1, 1, 1, 1)",
        ((f"目录/作者&平台&视频{i}.mp4", f"input/目录/作者&平台&视频{i}.mp4", i, i) for i in range(5000)),
    )
    db.executemany(
        "INSERT INTO task_logs (task_id, task_name, ts, level, message) VALUES (?, ?, ?, 'info', ?)",
        ((i % 5000 + 1, f"视频{i % 5000}", i, "处理记录 " * 20) for i in range(100_000)),
    )
    db.execute("COMMIT")

    def timed(fn):  # 跑 5 次，取中位数判断，同时打印最慢一次
        runs = []
        for _ in range(5):
            begin = time.perf_counter()
            result = fn()
            runs.append((time.perf_counter() - begin) * 1000)
        return result, statistics.median(runs), max(runs)

    tasks, list_ms, list_max = timed(lambda: repo.list_tasks(db))
    page, page_ms, page_max = timed(lambda: repo.list_logs_before(db, 2500, None, 100))
    older, older_ms, older_max = timed(lambda: repo.list_logs_before(db, 2500, page[-1]["id"], 100))

    print(f"\n读取全部 {len(tasks)} 个任务：中位数 {list_ms:.1f} ms，最慢 {list_max:.1f} ms；"
          f"按任务取一页日志：中位数 {page_ms:.2f} ms，最慢 {page_max:.2f} ms；"
          f"再往前翻一页：中位数 {older_ms:.2f} ms，最慢 {older_max:.2f} ms")
    assert len(tasks) == 5000 and len(page) == 20 and older == []
    assert list_ms < 100 and page_ms < 20 and older_ms < 20
