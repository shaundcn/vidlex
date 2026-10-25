"""队列控制、休眠、网络恢复、停止、关机（立项 4.1、8.1、8.2，技术选型 13.4、13.5）。"""

import asyncio
import shutil
from pathlib import Path

import pytest

from app.db import repo
from app.domain.errors import Conflict, GlobalError, KEY_INVALID, TaskFailed
from app.services import runtime as runtime_module
from app.services.clock import Clock
from app.services.cloud import Cloud
from app.services.runtime import Runtime
from app.services.scanner import Scanner
from app.services.worker import Worker
from fakes import FakeArk, FakeClock
from world import World

pytestmark = pytest.mark.anyio


class Env:
    def __init__(self, tmp_path: Path, clock=None) -> None:
        self.clock = clock or FakeClock()
        self.w = World(tmp_path, self.clock)
        self.ark = FakeArk(self.clock if isinstance(self.clock, FakeClock) else FakeClock())
        self.w.conn.execute("UPDATE settings SET api_key = 'ark-key-0000' WHERE id = 1")
        self.build()

    def build(self) -> None:
        cloud = Cloud(self.w.conn, self.ark, self.clock)
        worker = Worker(self.w.conn, self.w.lib, self.w.journal, cloud, self.ark, self.clock, self.w.db_path.parent)
        self.rt = Runtime(self.w.conn, self.w.lib, self.w.journal, Scanner(self.w.conn, self.w.lib, self.clock),
                          worker, cloud, self.ark, self.clock)

    def state(self):
        return repo.get_runtime_state(self.w.conn)

    def add_video(self, rel: str, samples) -> None:
        dst = self.w.file(f"input/{rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(samples["lc.mp4"], dst)


@pytest.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.w.start()
    return e


# ---- 休眠（立项 4.1、第 5 节）----

async def test_enter_sleep_after_idle_and_wake_on_new_file(env: Env, samples) -> None:
    await env.rt.scan_tick()
    env.clock.advance(29 * 60)
    await env.rt.scan_tick()
    assert env.state()["queue_state"] == "running"
    env.clock.advance(60)
    await env.rt.scan_tick()
    assert env.state()["queue_state"] == "sleeping"
    assert env.rt._scan_interval() == 2 * 3600
    # 休眠中扫描没有新文件：不写数据库（updated_at 不变），保持休眠
    before = env.state()["updated_at"]
    env.clock.advance(2 * 3600)
    await env.rt.scan_tick()
    assert env.state()["queue_state"] == "sleeping" and env.state()["updated_at"] == before
    env.add_video("a.mp4", samples)
    await env.rt.scan_tick()
    assert env.state()["queue_state"] == "running" and env.rt._scan_interval() == 180


@pytest.mark.parametrize("blocker", ["pending_file", "queued_task", "paused", "error", "disabled"])
async def test_no_sleep_when_busy(env: Env, samples, blocker: str) -> None:
    if blocker == "pending_file":
        env.add_video("a.mp4", samples)
    elif blocker == "queued_task":
        env.add_video("a.mp4", samples)
        env.w.enqueue("a.mp4")
    elif blocker == "paused":
        env.rt.pause()
    elif blocker == "error":
        env.rt.enter_error(GlobalError(KEY_INVALID))
    else:
        env.w.conn.execute("UPDATE settings SET sleep_enabled = 0 WHERE id = 1")
    for _ in range(2):
        env.clock.advance(31 * 60)
        await env.rt.scan_tick()
    assert env.state()["queue_state"] != "sleeping"


async def test_sleep_scan_does_cleanup(env: Env) -> None:
    repo.add_cloud_file(env.w.conn, "file-old", env.clock.now_ms() + 10**9, 1)
    env.ark.files["file-old"] = {}
    repo.set_queue_state(env.w.conn, "sleeping", None, False, False, 1)
    await env.rt.cloud_tick()
    assert repo.list_cloud_files(env.w.conn) != []  # 平时的清理循环在休眠时不动
    await env.rt.scan_tick()
    assert repo.list_cloud_files(env.w.conn) == []  # 等扫描时一起做


async def test_user_wake(env: Env) -> None:
    repo.set_queue_state(env.w.conn, "sleeping", None, False, False, 1)
    env.rt.wake()
    assert env.state()["queue_state"] == "running"
    with pytest.raises(Conflict):
        env.rt.wake()


# ---- 执行器、暂停、连续失败 ----

async def test_pause_blocks_new_tasks(env: Env, samples) -> None:
    env.add_video("a.mp4", samples)
    task_id = env.w.enqueue("a.mp4")
    env.rt.pause()
    assert await env.rt.executor_tick() is False
    await env.rt.resume()
    assert await env.rt.executor_tick() is True
    assert env.w.task(task_id)["status"] == "completed"


async def test_three_failures_stop_queue(env: Env, samples) -> None:
    ids = []
    for name in ("a", "b", "c", "d"):
        env.add_video(f"{name}.mp4", samples)
        ids.append(env.w.enqueue(f"{name}.mp4"))
    env.ark.fail("analyze", *[TaskFailed("内容审核没通过") for _ in range(3)])
    for _ in range(4):
        await env.rt.executor_tick()
    state = env.state()
    assert (state["queue_state"], state["error_message"]) == ("error", "连续 3 个任务失败")
    assert env.w.task(ids[3])["status"] == "queued"  # 第 4 个没有开始
    await env.rt.resume()
    assert env.state()["consecutive_failures"] == 0


async def test_success_resets_failures(env: Env, samples) -> None:
    for name in ("a", "b", "c"):
        env.add_video(f"{name}.mp4", samples)
        env.w.enqueue(f"{name}.mp4")
    env.ark.fail("analyze", TaskFailed("x"), TaskFailed("y"))
    for _ in range(3):
        await env.rt.executor_tick()
    assert env.state()["consecutive_failures"] == 0 and env.state()["queue_state"] == "running"


# ---- 停止、出错时中断当前任务 ----

async def start_blocked(env: Env, samples) -> tuple[int, asyncio.Task]:
    env.add_video("a.mp4", samples)
    task_id = env.w.enqueue("a.mp4")
    started = asyncio.Event()

    async def block():
        started.set()
        await asyncio.Event().wait()

    env.ark.on_analyze = block
    tick = asyncio.create_task(env.rt.executor_tick())
    await asyncio.wait_for(started.wait(), 10)  # 出问题时报错，不卡死
    return task_id, tick


async def test_stop_processing_task(env: Env, samples) -> None:
    task_id, tick = await start_blocked(env, samples)
    assert await env.rt.stop_task(task_id) is True
    await tick
    task = env.w.task(task_id)
    assert (task["status"], task["current_video_path"]) == ("stopped", "input/a.mp4")
    assert env.ark.files == {} and env.w.tmp_entries() == []


async def test_stop_refused_while_committing(env: Env, samples) -> None:
    task_id, tick = await start_blocked(env, samples)
    env.w.conn.execute("UPDATE tasks SET step = 'commit' WHERE id = ?", (task_id,))
    with pytest.raises(Conflict, match="正在保存结果"):
        await env.rt.stop_task(task_id)
    env.w.conn.execute("UPDATE tasks SET step = 'analyze' WHERE id = ?", (task_id,))
    await env.rt.stop_task(task_id)
    await tick


async def test_external_global_error_requeues_current(env: Env, samples) -> None:
    task_id, tick = await start_blocked(env, samples)
    env.rt.enter_error(GlobalError("存储故障", storage=True))
    await tick
    task = env.w.task(task_id)
    assert (task["status"], task["attempts"]) == ("queued", 0)
    assert env.rt.storage_fault
    await env.rt.scan_tick()  # 存储故障时停止扫描
    assert env.state()["queue_state"] == "error"


async def test_shutdown_leaves_task_interrupted(env: Env, samples) -> None:
    task_id, tick = await start_blocked(env, samples)
    env.rt.worker.shutting_down = True
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    assert env.w.task(task_id)["status"] == "processing"
    assert await env.w.restart()  # 下次启动退回排队最前，尝试次数加 1
    assert (env.w.task(task_id)["status"], env.w.task(task_id)["attempts"]) == ("queued", 1)


# ---- 存储故障、网络恢复 ----

async def test_storage_fault_from_scan_then_resume(env: Env) -> None:
    shutil.rmtree(env.w.file("input"))
    await env.rt.scan_tick()
    assert env.state()["queue_state"] == "error" and env.rt.storage_fault
    await env.rt.resume()  # 重新执行启动第 2–5 步：重新建出 input
    assert env.state()["queue_state"] == "running" and env.w.file("input").is_dir()
    assert not env.rt.storage_fault


async def test_resume_fails_while_still_broken(env: Env) -> None:
    env.w.file(".vidlex/library-id").write_text("other\n")
    env.rt.enter_error(GlobalError("存储故障", storage=True))
    with pytest.raises(Conflict, match="标识与记录不一致"):
        await env.rt.resume()
    assert env.state()["queue_state"] == "error"


async def test_network_recovery_auto_resumes(env: Env) -> None:
    env.rt.enter_error(GlobalError("无法连接方舟", auto_resume=True))
    env.ark.online = False
    await env.rt.network_tick()
    assert env.state()["queue_state"] == "error"
    await env.rt.cloud_tick()  # 网络类出错时云端清理暂停（不报错）
    env.ark.online = True
    await env.rt.network_tick()
    assert env.state()["queue_state"] == "running"


async def test_non_network_error_not_auto_resumed(env: Env) -> None:
    env.rt.enter_error(GlobalError(KEY_INVALID))
    await env.rt.network_tick()
    assert env.state()["queue_state"] == "error"


# ---- 真实循环的集成测试 ----

async def test_real_loops_end_to_end(tmp_path: Path, samples, monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "SCAN_INTERVAL_S", 0.05)
    env = Env(tmp_path, Clock())
    await env.w.start()
    env.add_video("教程/设计师小王&抖音&PS抠头发教程.mp4", samples)
    env.rt.start()
    try:
        for _ in range(200):
            await asyncio.sleep(0.05)
            done = [t for t in repo.list_tasks(env.w.conn) if t["status"] == "completed"]
            if done:
                break
        assert done and done[0]["current_video_path"] == "output/video/3 步用 PS 快速抠出发丝.mp4"
    finally:
        await env.rt.shutdown()


# ---- 两件事同时发生（阶段 3.3）----

async def test_global_error_during_commit_then_resume(env: Env, samples, monkeypatch) -> None:
    """保存结果做到一半（md 已就位、视频还没移）时队列进入出错：点继续后从记录接着做，只保存一次。"""
    env.add_video("a.mp4", samples)
    task_id = env.w.enqueue("a.mp4")
    steps = env.w.journal._steps
    key = ("commit", "md_placed")
    real_step = steps[key]

    async def error_midway(op):
        steps[key] = real_step
        env.rt.enter_error(GlobalError("存储故障", storage=True))
        await asyncio.sleep(0)  # 当前任务在这里被取消
        await real_step(op)

    monkeypatch.setitem(steps, key, error_midway)
    await env.rt.executor_tick()
    op = repo.get_op(env.w.conn, task_id)
    assert op is not None and op["phase"] == "md_placed"
    assert env.w.task(task_id)["status"] == "processing" and env.w.files(".mp4") == ["input/a.mp4"]
    await env.rt.resume()
    task = env.w.task(task_id)
    assert task["status"] == "completed" and repo.list_ops(env.w.conn) == []
    assert env.w.files(".mp4") == [task["current_video_path"]] and env.w.files(".md") == [task["md_path"]]
    assert env.state()["queue_state"] == "running"


async def test_shutdown_while_analyzing_then_cleanup_after_restart(env: Env, samples) -> None:
    """关机时不联网删除副本；重启后它不再算正在使用，后台清理把它删掉。"""
    task_id, tick = await start_blocked(env, samples)
    env.rt.worker.shutting_down = True
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    [file_id] = [r["file_id"] for r in repo.list_cloud_files(env.w.conn)]
    assert file_id in env.ark.files
    assert await env.w.restart()
    env.build()  # 重启：新的进程，内存里没有正在使用的副本
    await env.rt.cloud_tick()
    assert repo.list_cloud_files(env.w.conn) == [] and env.ark.files == {}
    assert env.w.task(task_id)["status"] == "queued"
