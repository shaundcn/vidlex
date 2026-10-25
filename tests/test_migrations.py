"""迁移执行器与版本检查（技术选型 4.8、4.13）。"""

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.db import connection


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    target = tmp_path / "migrations"
    target.mkdir()
    shutil.copy(connection.MIGRATIONS_DIR / "001_init.sql", target)
    return target


def test_broken_migration_rolls_back(tmp_path: Path, migrations: Path) -> None:
    db_path = tmp_path / "vidlex.db"
    connection.open_database(db_path, migrations).close()
    # 前半句成功、后半句失败：要验证前半句也被回滚
    (migrations / "002_broken.sql").write_text(
        "CREATE TABLE extra (a INTEGER) STRICT;\n"
        "ALTER TABLE tasks ADD COLUMN extra_col INTEGER;\n"
        "INSERT INTO no_such_table VALUES (1);\n"
    )
    before_tables = tables(db_path)
    with pytest.raises(connection.MigrationFailed):
        connection.open_database(db_path, migrations)
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    conn.close()
    assert tables(db_path) == before_tables
    assert "extra_col" not in columns


def test_broken_first_migration_leaves_empty_db(tmp_path: Path) -> None:
    migrations = tmp_path / "m"
    migrations.mkdir()
    (migrations / "001_init.sql").write_text("CREATE TABLE a (x INTEGER) STRICT; SELECT * FROM nope;")
    db_path = tmp_path / "vidlex.db"
    with pytest.raises(connection.MigrationFailed):
        connection.open_database(db_path, migrations)
    assert tables(db_path) == set()


def test_foreign_key_violation_rolls_back(tmp_path: Path, migrations: Path) -> None:
    db_path = tmp_path / "vidlex.db"
    connection.open_database(db_path, migrations).close()
    (migrations / "002_fk.sql").write_text(
        "INSERT INTO task_ops (task_id, kind, phase, src_path, src_dev, src_ino, src_size,"
        " src_mtime_ns, video_target_path, created_at, updated_at)"
        " VALUES (999, 'ignore', 'recorded', 'a', 1, 1, 1, 1, 'b', 1, 1);"
    )
    with pytest.raises(connection.MigrationFailed, match="外键"):
        connection.open_database(db_path, migrations)
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM task_ops").fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def test_newer_database_is_refused_and_untouched(tmp_path: Path) -> None:
    db_path = tmp_path / "vidlex.db"
    conn = connection.open_database(db_path)
    conn.execute("PRAGMA user_version=99")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = digest(db_path)
    with pytest.raises(connection.DatabaseTooNew):
        connection.open_database(db_path)
    assert digest(db_path) == before


def test_002_moves_audio_only_notice_into_analysis_mode(tmp_path: Path, migrations: Path) -> None:
    """NAS 上任务 31 这类旧数据：只用音频分析的提示转成字段，其他已完成任务记为 video。"""
    db_path = tmp_path / "vidlex.db"
    conn = connection.open_database(db_path, migrations)  # 只有 001
    for i, (status, notice) in enumerate([("completed", "超过 512MB，只用音频分析"), ("completed", None),
                                          ("failed", None), ("completed", "文件不存在")], start=1):
        conn.execute(
            "INSERT INTO tasks (orig_rel_path, current_video_path, md_path, title, status, fail_reason, notice,"
            " src_dev, src_ino, src_size, src_mtime_ns, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 1, 1, 1, 1)",
            (f"{i}.mp4", f"v/{i}.mp4", f"m/{i}.md" if status == "completed" else None,
             "t" if status == "completed" else None, status, "x" if status == "failed" else None, notice, i))
    conn.close()
    conn = connection.open_database(db_path)  # 全部迁移
    rows = conn.execute("SELECT analysis_mode, notice FROM tasks ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("audio", None), ("video", None), (None, None), ("video", "文件不存在")]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tasks SET analysis_mode = 'both' WHERE id = 1")
    conn.close()
