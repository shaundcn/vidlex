"""4 种文件操作记录：每个持久化动作之后模拟断电，重启后检查恢复结果（技术选型 13.14 阶段 3a）。"""

import errno
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from app.db import repo
from app.domain import naming
from app.domain.errors import Conflict, GlobalError, NotFound
from world import ORIG, TITLE, PowerLoss, World, power_loss_after

pytestmark = pytest.mark.anyio

MD = f"output/{TITLE}.md"
VIDEO_DONE = f"output/video/{TITLE}.mp4"
VIDEO_IN = f"input/{ORIG}"
VIDEO_IGNORED = "output/ignored/设计师小王&抖音&PS抠头发教程.mp4"


def assert_clean(w: World, task_id: int) -> None:
    """不剩进度记录、临时目录已清理、队列正常，视频只有一份且就在任务记录的位置。"""
    assert repo.list_ops(w.conn) == []
    assert w.tmp_entries() == []
    assert repo.get_runtime_state(w.conn)["queue_state"] == "running"
    task = w.task(task_id)
    videos = w.files(".mp4")
    assert videos == [task["current_video_path"]], videos


async def crash_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Callable[[World], Awaitable[int]],
    action: Callable[[World, int], Awaitable[object]],
    check: Callable[[World, int, bool], None],
) -> int:
    """k = 1, 2, … 依次在第 k 个持久化动作后断电，直到操作不中断地完成。返回断电点数量。"""
    k = 1
    while True:
        w = World(tmp_path / f"k{k}")
        await w.start()
        task_id = await setup(w)
        with power_loss_after(monkeypatch, k):
            try:
                await action(w, task_id)
                crashed = False
            except PowerLoss:
                crashed = True
        # 记录是否已经落盘：有进度记录，或者已经完成
        task = w.task(task_id)
        recorded = repo.get_op(w.conn, task_id) is not None or (task is not None and task["updated_at"] > 2)
        assert await w.restart()
        check(w, task_id, recorded or not crashed)
        if not crashed:
            return k
        k += 1


# ---- commit ----

async def setup_processing(w: World) -> int:
    task_id = w.enqueue()
    w.set_processing(task_id)
    return task_id


async def do_commit(w: World, task_id: int) -> None:
    w.enter_commit(task_id)
    await w.journal.run_commit(task_id)


def check_commit(w: World, task_id: int, done: bool) -> None:
    assert_clean(w, task_id)
    task = w.task(task_id)
    if done:
        assert (task["status"], task["title"], task["md_path"]) == ("completed", TITLE, MD)
        assert task["current_video_path"] == VIDEO_DONE
        assert w.files(".md") == [MD]
        assert w.file(MD).read_text() == f"![[{TITLE}.mp4]]\n"
    else:
        # 结果提交前断电：退回排队最前，尝试次数加 1，从头处理（立项 8.2 第 1 行）
        assert (task["status"], task["attempts"], task["current_video_path"]) == ("queued", 1, VIDEO_IN)
        assert w.files(".md") == []


async def test_commit_power_loss_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    points = await crash_loop(tmp_path, monkeypatch, setup_processing, do_commit, check_commit)
    print(f"\ncommit：{points - 1} 个断电点")
    assert points > 8


# ---- redo ----

async def setup_completed(w: World) -> int:
    task_id = await setup_processing(w)
    await do_commit(w, task_id)
    w.conn.execute("UPDATE tasks SET updated_at = 2 WHERE id = ?", (task_id,))
    return task_id


async def do_redo(w: World, task_id: int) -> None:
    await w.journal.redo(task_id)


def check_redo(w: World, task_id: int, done: bool) -> None:
    assert_clean(w, task_id)
    task = w.task(task_id)
    if done:
        assert (task["status"], task["title"], task["md_path"], task["attempts"]) == ("queued", None, None, 0)
        assert task["current_video_path"] == VIDEO_IN
        assert w.files(".md") == []  # 旧 md 已删除
    else:
        assert (task["status"], task["current_video_path"]) == ("completed", VIDEO_DONE)
        assert w.files(".md") == [MD]


async def test_redo_power_loss_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    points = await crash_loop(tmp_path, monkeypatch, setup_completed, do_redo, check_redo)
    print(f"\nredo：{points - 1} 个断电点")
    assert points > 5


# ---- ignore / unignore ----

async def setup_queued(w: World) -> int:
    task_id = w.enqueue()
    w.conn.execute("UPDATE tasks SET updated_at = 2 WHERE id = ?", (task_id,))
    return task_id


async def do_ignore(w: World, task_id: int) -> None:
    await w.journal.ignore(task_id)


def check_ignore(w: World, task_id: int, done: bool) -> None:
    assert_clean(w, task_id)
    task = w.task(task_id)
    expected = ("ignored", VIDEO_IGNORED) if done else ("queued", VIDEO_IN)
    assert (task["status"], task["current_video_path"]) == expected


async def test_ignore_power_loss_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    points = await crash_loop(tmp_path, monkeypatch, setup_queued, do_ignore, check_ignore)
    print(f"\nignore：{points - 1} 个断电点")
    assert points > 4


async def setup_ignored(w: World) -> int:
    task_id = await setup_queued(w)
    await w.journal.ignore(task_id)
    w.conn.execute("UPDATE tasks SET updated_at = 2 WHERE id = ?", (task_id,))
    return task_id


async def do_unignore(w: World, task_id: int) -> None:
    await w.journal.unignore(task_id)


def check_unignore(w: World, task_id: int, done: bool) -> None:
    assert_clean(w, task_id)
    task = w.task(task_id)
    expected = ("queued", VIDEO_IN) if done else ("ignored", VIDEO_IGNORED)
    assert (task["status"], task["current_video_path"]) == expected


async def test_unignore_power_loss_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    points = await crash_loop(tmp_path, monkeypatch, setup_ignored, do_unignore, check_unignore)
    print(f"\nunignore：{points - 1} 个断电点")
    assert points > 4


# ---- 断电后源视频又被替换或删除（立项 8.2 第 2 行、8.6）----

@pytest.mark.parametrize("change", ["replace", "delete"])
async def test_commit_power_loss_then_source_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    k = 1
    while True:
        w = World(tmp_path / f"{change}{k}")
        await w.start()
        task_id = await setup_processing(w)
        original_ino = w.file(VIDEO_IN).stat().st_ino
        with power_loss_after(monkeypatch, k):
            try:
                await do_commit(w, task_id)
                crashed = False
            except PowerLoss:
                crashed = True
        recorded = repo.get_op(w.conn, task_id) is not None or w.task(task_id)["status"] == "completed"
        moved = w.file(VIDEO_DONE).exists()
        src = w.file(VIDEO_IN)
        if src.exists():
            src.unlink()
        if change == "replace":
            src.write_bytes(b"new-video")
        assert await w.restart()
        task = w.task(task_id)
        if not recorded:
            assert task["status"] == "queued"  # 还没提交：交给扫描器处理（阶段 3b）
        elif moved:
            assert task["status"] == "completed" and task["current_video_path"] == VIDEO_DONE
            assert w.file(VIDEO_DONE).stat().st_ino == original_ino
        elif change == "replace":
            assert task is None  # 旧任务消失，丢弃结果，不移动新文件
            assert w.files(".md") == []
        else:
            assert task["status"] == "completed" and task["current_video_path"] is None
            assert w.files(".md") == [MD]  # md 保留
        if change == "replace":
            assert src.read_bytes() == b"new-video"  # 新文件原样留在 input
        assert repo.list_ops(w.conn) == [] and w.tmp_entries() == []
        if not crashed:
            break
        k += 1


# ---- 其他异常分支 ----

async def test_ignore_numbering_and_db_reserved_names(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    w.file(VIDEO_IGNORED).write_bytes(b"other")
    task_id = w.enqueue()
    # 另一条进度记录已占用 (1)：也要跳过
    other = w.enqueue("另一个.mp4")
    w.conn.execute(
        "INSERT INTO task_ops (task_id, kind, phase, src_path, src_dev, src_ino, src_size, src_mtime_ns,"
        " video_target_path, created_at, updated_at) VALUES (?, 'ignore', 'recorded', 'input/另一个.mp4',"
        " 0, 0, 0, 0, 'output/ignored/设计师小王&抖音&PS抠头发教程(1).mp4', 1, 1)", (other,))
    task = await w.journal.ignore(task_id)
    assert task["current_video_path"] == "output/ignored/设计师小王&抖音&PS抠头发教程(2).mp4"
    assert w.file(VIDEO_IGNORED).read_bytes() == b"other"


async def test_ignore_refused_when_file_replaced_or_missing(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = w.enqueue()
    w.file(VIDEO_IN).unlink()
    w.file(VIDEO_IN).write_bytes(b"replaced")
    with pytest.raises(Conflict, match="文件已变化"):
        await w.journal.ignore(task_id)
    assert w.task(task_id)["notice"] == "文件已变化，请等待重新扫描"
    w.file(VIDEO_IN).unlink()
    with pytest.raises(Conflict, match="文件不存在"):
        await w.journal.ignore(task_id)
    assert repo.list_ops(w.conn) == [] and w.task(task_id)["status"] == "queued"


async def test_user_move_failure_keeps_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = w.enqueue()

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "link", denied)
    with pytest.raises(Conflict, match="剪切失败"):
        await w.journal.ignore(task_id)
    task = w.task(task_id)
    assert task["status"] == "queued" and task["notice"].startswith("剪切失败")
    assert repo.list_ops(w.conn) == [] and w.files(".mp4") == [VIDEO_IN]
    assert repo.get_runtime_state(w.conn)["queue_state"] == "running"  # 不影响队列


async def test_redo_old_md_undeletable_then_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = await setup_completed(w)
    original_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if str(path).endswith(".md"):
            raise PermissionError(13, "Permission denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(GlobalError):
        await w.journal.redo(task_id)
    assert repo.get_op(w.conn, task_id)["phase"] == "video_restored"
    assert w.task(task_id)["current_video_path"] == VIDEO_IN
    monkeypatch.undo()
    assert await w.restart()  # 修好后从这一步继续，不重复剪切
    check_redo(w, task_id, done=True)


async def test_redo_when_video_deleted(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = await setup_completed(w)
    w.file(VIDEO_DONE).unlink()
    with pytest.raises(Conflict, match="文件不存在"):
        await w.journal.redo(task_id)
    task = w.task(task_id)
    assert task["status"] == "completed" and task["notice"] == "文件不存在"
    assert w.files(".md") == [MD]  # 旧 md 不删


async def test_unignore_replaced_file_uses_current_file(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = await setup_ignored(w)
    w.file(VIDEO_IGNORED).unlink()
    w.file(VIDEO_IGNORED).write_bytes(b"replaced-in-ignored")
    task = await w.journal.unignore(task_id)
    assert task["status"] == "queued"
    assert (task["src_ino"], task["src_size"]) == (w.file(VIDEO_IN).stat().st_ino, len(b"replaced-in-ignored"))


async def test_unignore_restores_into_recreated_dir_with_number(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = await setup_ignored(w)
    w.file("input/教程").rmdir()
    await w.start()  # input 被清空后正常启动
    w.add_video(ORIG, b"someone-else")
    task = await w.journal.unignore(task_id)
    assert task["current_video_path"] == "input/教程/设计师小王&抖音&PS抠头发教程(1).mp4"
    assert w.file(VIDEO_IN).read_bytes() == b"someone-else"


async def test_commit_target_taken_enters_error(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = await setup_processing(w)
    w.enter_commit(task_id)
    w.file(VIDEO_DONE).write_bytes(b"user-created")  # 记录写好后用户抢先放了同名文件
    with pytest.raises(GlobalError, match="目标位置被占用"):
        await w.journal.run_commit(task_id)
    assert w.file(VIDEO_DONE).read_bytes() == b"user-created"  # 不覆盖
    assert await w.restart() is False
    assert repo.get_runtime_state(w.conn)["queue_state"] == "error"
    w.file(VIDEO_DONE).unlink()
    assert await w.start()  # 移走后点继续：从记录继续，不重新分析
    assert w.task(task_id)["status"] == "completed"


async def test_commit_md_lost_enters_error_without_deleting(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = await setup_processing(w)
    w.enter_commit(task_id)
    w.file(f".vidlex/tmp/{task_id}/note.md").unlink()
    assert await w.restart() is False
    state = repo.get_runtime_state(w.conn)
    assert state["queue_state"] == "error" and "找不到要保存的 md" in state["error_message"]
    assert repo.get_op(w.conn, task_id) is not None and w.files(".mp4") == [VIDEO_IN]


# ---- 异常分支：不覆盖、不丢文件，读写出错按存储问题处理 ----

async def test_commit_md_target_taken_by_other_file(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = await setup_processing(w)
    w.enter_commit(task_id)
    w.file(MD).parent.mkdir(parents=True, exist_ok=True)
    w.file(MD).write_text("用户自己的笔记")  # 记录写好后，md 目标位置被另一个文件占了
    with pytest.raises(GlobalError, match="目标位置被占用"):
        await w.journal.run_commit(task_id)
    assert w.file(MD).read_text() == "用户自己的笔记"  # 不覆盖
    assert repo.get_op(w.conn, task_id) is not None and w.files(".mp4") == [VIDEO_IN]


async def test_commit_video_move_io_error_is_storage_fault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = await setup_processing(w)
    w.enter_commit(task_id)
    real_move = w.lib.move

    def failing_move(src: str, dst: str) -> None:
        if dst.startswith("output/video/"):
            raise OSError(errno.EIO, "Input/output error")
        real_move(src, dst)

    monkeypatch.setattr(w.lib, "move", failing_move)
    with pytest.raises(GlobalError) as caught:
        await w.journal.run_commit(task_id)
    assert caught.value.storage and w.files(".mp4") == [VIDEO_IN] and w.files(".md") == [MD]
    monkeypatch.undo()
    assert await w.restart()  # 硬盘恢复后从记录继续，不重复放 md
    assert w.task(task_id)["status"] == "completed"
    assert_clean(w, task_id)


async def test_recover_ignore_when_file_replaced_keeps_task(tmp_path: Path) -> None:
    """记录写好后、移动前视频被替换：恢复时放弃这次忽略，任务保持原状并提示。"""
    w = World(tmp_path)
    await w.start()
    task_id = w.enqueue()
    stale = (1, 2, 3, 4)
    repo.begin_op(w.conn, task_id, "ignore", VIDEO_IN, stale, VIDEO_IGNORED, None, 5)
    assert await w.restart()
    task = w.task(task_id)
    assert (task["status"], task["notice"]) == ("queued", "文件已变化，请等待重新扫描")
    assert repo.list_ops(w.conn) == [] and w.files(".mp4") == [VIDEO_IN]


async def test_recover_redo_when_video_missing_aborts(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = await setup_processing(w)
    await do_commit(w, task_id)
    ident = repo.row_ident(w.task(task_id))
    repo.begin_op(w.conn, task_id, "redo", VIDEO_DONE, ident, VIDEO_IN, MD, 5)
    w.file(VIDEO_DONE).unlink()
    assert await w.restart()
    task = w.task(task_id)
    assert (task["status"], task["notice"]) == ("completed", "文件不存在")
    assert w.files(".md") == [MD]  # 旧 md 不删


async def test_recover_ignore_when_video_missing_removes_task(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = w.enqueue()
    ident = repo.row_ident(w.task(task_id))
    repo.begin_op(w.conn, task_id, "ignore", VIDEO_IN, ident, VIDEO_IGNORED, None, 5)
    w.file(VIDEO_IN).unlink()
    assert await w.restart()
    assert w.task(task_id) is None and repo.list_ops(w.conn) == []


async def test_operation_guards(tmp_path: Path) -> None:
    w = World(tmp_path)
    await w.start()
    with pytest.raises(NotFound):
        await w.journal.ignore(9999)
    task_id = await setup_processing(w)
    w.enter_commit(task_id)
    with pytest.raises(Conflict, match="正在执行上一个操作"):
        await w.journal.ignore(task_id)


async def test_too_many_same_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(tmp_path)
    await w.start()
    task_id = w.enqueue()
    monkeypatch.setattr(naming, "MAX_NUMBER", 1)
    for name in ("设计师小王&抖音&PS抠头发教程.mp4", "设计师小王&抖音&PS抠头发教程(1).mp4"):
        w.file(f"output/ignored/{name}").write_bytes(b"x")
    with pytest.raises(Conflict, match="同名文件太多"):
        await w.journal.ignore(task_id)
    assert w.task(task_id)["status"] == "queued" and w.files(".mp4").count(VIDEO_IN) == 1
