"""启动顺序第 2–5 步（技术选型 13.4）和立项 8.2 中属于启动的几行。"""

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.config import Config
from app.db import connection, repo
from app.domain.errors import STORAGE_FAULT
from app.services import startup
from app.services.fsops import LIBRARY_ID_FILE
from app.services.startup import INTERRUPTED_TOO_OFTEN
from world import PowerLoss, World, power_loss_after

pytestmark = pytest.mark.anyio


def tables(w: World) -> dict[str, list[tuple]]:
    return {t: [tuple(r) for r in w.conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
            for t in ("tasks", "task_ops")}


def tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


async def test_empty_input_is_normal(tmp_path: Path) -> None:
    w = World(tmp_path)
    assert await w.start()
    for rel in ("input", "output", "output/video", "output/ignored", ".vidlex/tmp"):
        assert w.file(rel).is_dir()
    state = repo.get_runtime_state(w.conn)
    assert state["queue_state"] == "running" and state["library_id"] == w.file(LIBRARY_ID_FILE).read_text().strip()


async def test_interrupted_task_requeued_front_and_tmp_removed(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    first = w.enqueue("a.mp4")
    second = w.enqueue("b.mp4")
    w.set_processing(second, step="transcode")
    w.lib.write_atomic(f".vidlex/tmp/{second}/upload.mp4", "half")  # 音频转码到一半
    assert await w.restart()
    task = w.task(second)
    assert (task["status"], task["step"], task["attempts"]) == ("queued", None, 1)
    assert task["queue_seq"] < w.task(first)["queue_seq"]
    assert w.tmp_entries() == []


async def test_interrupted_too_often_fails(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = w.enqueue()
    w.set_processing(task_id, attempts=3)
    assert await w.restart()
    task = w.task(task_id)
    assert (task["status"], task["fail_reason"], task["attempts"]) == ("failed", INTERRUPTED_TOO_OFTEN, 4)
    assert repo.get_runtime_state(w.conn)["consecutive_failures"] == 1


async def test_interrupted_third_consecutive_failure_enters_error(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    w.conn.execute("UPDATE runtime_state SET consecutive_failures = 2 WHERE id = 1")
    task_id = w.enqueue()
    w.set_processing(task_id, attempts=3)
    await w.restart()
    state = repo.get_runtime_state(w.conn)
    assert (state["queue_state"], state["error_message"], state["consecutive_failures"]) == (
        "error", "连续 3 个任务失败", 3)


async def pending_ignore(w: World, monkeypatch: pytest.MonkeyPatch) -> int:
    """造一条做到一半的 ignore 记录：写完记录后断电。"""
    await w.start()
    task_id = w.enqueue()
    with power_loss_after(monkeypatch, 1):
        with pytest.raises(PowerLoss):
            await w.journal.ignore(task_id)
    assert repo.get_op(w.conn, task_id) is not None
    return task_id


@pytest.mark.parametrize("fault", ["id_mismatch", "id_file_missing", "not_mounted"])
async def test_storage_fault_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    w = World(tmp_path)
    task_id = await pending_ignore(w, monkeypatch)
    w.conn.execute("UPDATE tasks SET status = 'processing', step = 'upload', queue_seq = NULL"
                   " WHERE id = ?", (w.enqueue("b.mp4"),))  # 还有一个中断的任务
    id_file = w.file(LIBRARY_ID_FILE)
    if fault == "id_mismatch":
        id_file.write_text("another-library\n")
    elif fault == "id_file_missing":
        id_file.unlink()
    else:
        shutil.move(w.lib_dir, tmp_path / "unmounted")
    before_db = tables(w)
    before_files = tree(w.lib_dir) if w.lib_dir.exists() else {}
    assert await w.restart() is False
    state = repo.get_runtime_state(w.conn)
    assert state["queue_state"] == "error" and state["error_message"].startswith("存储故障")
    assert tables(w) == before_db  # 任务和进度记录完全不变
    assert (tree(w.lib_dir) if w.lib_dir.exists() else {}) == before_files
    if fault == "not_mounted":
        shutil.move(tmp_path / "unmounted", w.lib_dir)
    else:
        id_file.write_text(state["library_id"] + "\n")
    # 修好后点“继续”：重新执行第 2–5 步，记录按原样完成
    assert await w.start()
    assert w.task(task_id)["status"] == "ignored"
    assert repo.list_ops(w.conn) == []


async def test_storage_fault_while_paused_does_not_auto_resume(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    repo.set_queue_state(w.conn, "paused", None, False, False, 1)
    w.file(LIBRARY_ID_FILE).write_text("other\n")
    await w.restart()
    state = repo.get_runtime_state(w.conn)
    assert state["queue_state"] == "error" and state["error_auto_resume"] == 0


# ---- 第 1 步失败进入降级模式；第 2–5 步读写出错进入出错（技术选型 13.4）----

def test_degraded_when_data_dir_unusable(tmp_path: Path) -> None:
    (tmp_path / "data").write_text("这里本该是文件夹")  # data 被一个同名文件占住
    state = startup.init_database(Config(tmp_path / "data", tmp_path / "lib"), 1)
    assert (state.conn, state.degraded_reason) == (None, "data 文件夹无法读写")


def test_degraded_when_migration_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(path: Path) -> None:
        raise connection.MigrationFailed("002_x.sql：模拟失败")

    monkeypatch.setattr(connection, "open_database", failing)
    state = startup.init_database(Config(tmp_path / "data", tmp_path / "lib"), 1)
    assert (state.conn, state.degraded_reason) == (None, "数据库升级失败")


def test_degraded_when_singletons_unwritable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(repo, "ensure_singletons", broken)
    state = startup.init_database(Config(tmp_path / "data", tmp_path / "lib"), 1)
    assert (state.conn, state.degraded_reason) == (None, "数据库文件损坏")


async def test_library_write_error_enters_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(tmp_path)
    task_id = w.enqueue()
    w.set_processing(task_id)

    def denied() -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(w.lib, "ensure_layout", denied)
    assert await w.start() is False
    state = repo.get_runtime_state(w.conn)
    assert state["queue_state"] == "error" and state["error_message"] == STORAGE_FAULT
    assert w.task(task_id)["status"] == "processing"  # 第 2 步不通过时不改任何任务
