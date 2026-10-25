"""扫描（立项第 3 节、8.2、8.6，技术选型 13.6）。"""

import os
from pathlib import Path

import pytest

from app.db import repo
from app.domain.errors import GlobalError
from app.services.scanner import Scanner
from fakes import FakeClock
from world import World

pytestmark = pytest.mark.anyio

BAD = os.fsdecode(b"\xc4\xe3\xba\xc3.mp4")


@pytest.fixture
async def env(tmp_path: Path):
    clock = FakeClock()
    w = World(tmp_path, clock)
    await w.start()
    return w, Scanner(w.conn, w.lib, clock), clock


def add(w: World, rel: str, content: bytes = b"v") -> Path:
    path = w.file(f"input/{rel}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def tasks(w: World) -> dict[str, str]:
    return {t["current_video_path"]: t["status"] for t in repo.list_tasks(w.conn)}


async def scans(scanner: Scanner, n: int):
    for _ in range(n):
        result = await scanner.scan()
    return result


async def test_enqueued_after_three_unchanged_scans(env) -> None:
    w, scanner, _ = env
    add(w, "教程/a.mp4")
    first = await scanner.scan()
    assert first.new_files and first.pending and tasks(w) == {}
    await scanner.scan()
    third = await scanner.scan()
    assert third.enqueued == 1 and not third.pending
    task = repo.list_tasks(w.conn)[0]
    assert (task["orig_rel_path"], task["current_video_path"]) == ("教程/a.mp4", "input/教程/a.mp4")
    assert (await scanner.scan()).enqueued == 0  # 不重复入队


async def test_copy_in_progress_restarts_count(env) -> None:
    w, scanner, _ = env
    path = add(w, "a.mp4", b"1")
    await scans(scanner, 2)
    path.write_bytes(b"12")  # 还在复制，大小变了
    await scans(scanner, 2)
    assert tasks(w) == {}
    await scanner.scan()
    assert tasks(w) == {"input/a.mp4": "queued"}


async def test_restart_recounts(env, tmp_path: Path) -> None:
    w, scanner, clock = env
    add(w, "a.mp4")
    await scans(scanner, 2)
    fresh = Scanner(w.conn, w.lib, clock)  # 重启后计数只在内存里，重新计
    await scans(fresh, 2)
    assert tasks(w) == {}


async def test_filters(env) -> None:
    w, scanner, _ = env
    for rel in ("IMG_0001.MOV", "._IMG_0001.MOV", ".hidden.mp4", "a.mkv", "a.mp4.part",
                "@eaDir/x.mp4", "#recycle/y.mp4", ".git/z.mp4", BAD):
        add(w, rel)
    (w.file("input/link.mp4")).symlink_to(w.file("input/IMG_0001.MOV"))
    await scans(scanner, 3)
    assert tasks(w) == {"input/IMG_0001.MOV": "queued"}


async def test_bad_name_logged_once(env, caplog) -> None:
    w, scanner, _ = env
    add(w, BAD)
    await scans(scanner, 3)
    assert caplog.text.count("不是合法 UTF-8") == 1


@pytest.mark.parametrize("state", ["queued", "failed", "stopped"])
async def test_deleted_removes_task(env, state: str) -> None:
    w, scanner, _ = env
    task_id = w.enqueue("a.mp4")
    if state != "queued":
        w.conn.execute("UPDATE tasks SET status = ?, queue_seq = NULL, fail_reason = ? WHERE id = ?",
                       (state, "x" if state == "failed" else None, task_id))
    w.file("input/a.mp4").unlink()
    await scanner.scan()
    assert w.task(task_id) is None
    assert "视频已被删除或移走" in repo.list_logs_before(w.conn, task_id, None, 10)[0]["message"]


@pytest.mark.parametrize("state", ["queued", "failed", "stopped"])
async def test_replaced_becomes_new_video(env, state: str) -> None:
    w, scanner, clock = env
    task_id = w.enqueue("a.mp4")
    created = w.task(task_id)["created_at"]
    if state != "queued":
        w.conn.execute("UPDATE tasks SET status = ?, queue_seq = NULL, fail_reason = ? WHERE id = ?",
                       (state, "x" if state == "failed" else None, task_id))
    w.file("input/a.mp4").unlink()
    add(w, "a.mp4", b"replaced")
    clock.advance(60)
    await scanner.scan()
    assert w.task(task_id) is None and tasks(w) == {}
    await scans(scanner, 2)
    new = repo.list_tasks(w.conn)[0]
    assert new["id"] != task_id and new["status"] == "queued" and new["created_at"] > created


async def test_renamed_is_new_video(env) -> None:
    w, scanner, _ = env
    task_id = w.enqueue("a.mp4")
    w.file("input/sub").mkdir()
    w.file("input/a.mp4").rename(w.file("input/sub/b.mp4"))
    await scans(scanner, 3)
    assert w.task(task_id) is None and tasks(w) == {"input/sub/b.mp4": "queued"}


async def test_processing_and_op_tasks_untouched(env) -> None:
    w, scanner, _ = env
    processing = w.enqueue("a.mp4")
    w.set_processing(processing)
    w.file("input/a.mp4").unlink()
    with_op = w.enqueue("b.mp4")
    w.conn.execute("INSERT INTO task_ops (task_id, kind, phase, src_path, src_dev, src_ino, src_size,"
                   " src_mtime_ns, video_target_path, created_at, updated_at)"
                   " VALUES (?, 'ignore', 'recorded', 'input/b.mp4', 0, 0, 0, 0, 'output/ignored/b.mp4', 1, 1)",
                   (with_op,))
    w.file("input/b.mp4").unlink()
    await scanner.scan()
    assert w.task(processing) is not None and w.task(with_op) is not None


async def test_ignored_deleted_or_replaced(env) -> None:
    w, scanner, _ = env
    a = w.enqueue("a.mp4")
    b = w.enqueue("b.mp4")
    await w.journal.ignore(a)
    await w.journal.ignore(b)
    w.file("output/ignored/a.mp4").unlink()
    w.file("output/ignored/b.mp4").write_bytes(b"replaced")
    await scanner.scan()
    assert w.task(a) is None
    assert w.task(b)["status"] == "ignored"  # 被替换仍是已忽略


async def test_unreadable_subdir_keeps_its_tasks(env, monkeypatch) -> None:
    w, scanner, _ = env
    task_id = w.enqueue("sub/a.mp4")
    original = os.walk

    def walk(top, onerror=None, **kwargs):
        for dirpath, dirnames, filenames in original(top, onerror=onerror, **kwargs):
            if "sub" in dirnames:
                dirnames.remove("sub")
                onerror(PermissionError(13, "denied", os.path.join(dirpath, "sub")))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(os, "walk", walk)
    await scanner.scan()
    assert w.task(task_id) is not None  # 读不了的目录里的任务不当成被删除


async def test_input_missing_is_storage_fault(env) -> None:
    w, scanner, _ = env
    w.enqueue("a.mp4")
    for p in sorted(w.file("input").rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    w.file("input").rmdir()
    with pytest.raises(GlobalError) as info:
        await scanner.scan()
    assert info.value.storage and len(repo.list_tasks(w.conn)) == 1  # 不删除任务


async def test_empty_input_is_normal(env) -> None:
    _, scanner, _ = env
    result = await scanner.scan()
    assert not result.new_files and not result.pending


async def test_housekeeping(env) -> None:
    w, scanner, clock = env
    task_id = w.enqueue("a.mp4")
    repo.add_log(w.conn, task_id, "a", clock.now_ms() - 31 * 86400_000, "info", "很早的日志")
    repo.add_log(w.conn, task_id, "a", clock.now_ms() - 29 * 86400_000, "info", "近期的日志")
    processing = w.enqueue("b.mp4")
    w.set_processing(processing)
    for name in (str(task_id), str(processing)):
        w.lib.write_atomic(f".vidlex/tmp/{name}/x", "x")
    await scanner.housekeeping()
    assert [r["message"] for r in repo.list_logs_before(w.conn, task_id, None, 10)
            if "日志" in r["message"]] == ["近期的日志"]
    assert w.tmp_entries() == [str(processing)]
