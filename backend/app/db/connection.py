"""数据库连接、损坏检查、版本检查和迁移执行器（技术选型 4.3、4.8、4.9）。"""

import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_.+\.sql$")


class VidlexConnection(sqlite3.Connection):
    """带“提交后回调”的连接：repo 每次写事务提交后调用，参数表示是否改动了任务列表。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.commit_listeners: list[Callable[[bool], None]] = []


class DatabaseCorrupt(Exception):
    """quick_check 不通过，或文件根本不是 SQLite 数据库。"""


class DatabaseTooNew(Exception):
    """数据库版本比程序认识的新。"""


class MigrationFailed(Exception):
    """迁移执行失败，已整体回滚。"""


def list_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found = []
    for path in migrations_dir.iterdir():
        match = _MIGRATION_NAME.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort()
    return found


def open_database(path: Path, migrations_dir: Path = MIGRATIONS_DIR) -> sqlite3.Connection:
    """打开数据库并升级到程序认识的版本。失败时连接已关闭，数据库保持原样。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先以 600 权限建出空文件，避免 sqlite 按 umask 建出别人可读的文件
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    # autocommit=True：sqlite3 模块不自动开启或提交事务，全部由我们显式写 BEGIN / COMMIT
    conn = sqlite3.connect(path, autocommit=True, factory=VidlexConnection)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        _quick_check(conn)
        _check_version(conn, migrations_dir)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        _migrate(conn, migrations_dir)
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    for suffix in ("", "-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            os.chmod(sidecar, 0o600)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _quick_check(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorrupt(str(exc)) from exc
    if [row[0] for row in rows] != ["ok"]:
        raise DatabaseCorrupt("; ".join(str(row[0]) for row in rows))


def _check_version(conn: sqlite3.Connection, migrations_dir: Path) -> None:
    known = max((number for number, _ in list_migrations(migrations_dir)), default=0)
    current = schema_version(conn)
    if current > known:
        raise DatabaseTooNew(f"数据库版本 {current}，程序只认识到 {known}")


def _migrate(conn: sqlite3.Connection, migrations_dir: Path) -> None:
    for number, path in list_migrations(migrations_dir):
        if number <= schema_version(conn):
            continue
        sql = path.read_text(encoding="utf-8")
        # 外键检查的开关不能在事务内切换，所以放在事务外
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN")
            conn.executescript(sql)
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise MigrationFailed(f"{path.name} 执行后外键检查不通过：{len(problems)} 处")
            conn.execute(f"PRAGMA user_version={number}")
            conn.execute("COMMIT")
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(exc, MigrationFailed):
                raise
            raise MigrationFailed(f"{path.name}：{exc}") from exc
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
