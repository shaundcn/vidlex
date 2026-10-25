"""队列控制、4 个后台循环、休眠、网络检测（立项 4.1、8.1、8.2，技术选型 13.4、13.5）。

每个循环只是“执行一次 tick → 等待”；逻辑都在 tick 里，测试直接调用 tick。
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable

from app.db import repo
from app.domain import status
from app.domain.errors import Conflict, GlobalError
from app.services import startup, tasklog
from app.services.ark_client import ArkApi
from app.services.clock import Clock
from app.services.cloud import Cloud
from app.services.fsops import Library
from app.services.journal import Journal
from app.services.scanner import Scanner
from app.services.worker import Worker

log = logging.getLogger("vidlex.runtime")

SCAN_INTERVAL_S = 180
CLOUD_INTERVAL_S = 600
NETWORK_INTERVAL_S = 300
EXECUTOR_IDLE_S = 60


class Runtime:
    def __init__(
        self,
        conn: sqlite3.Connection,
        library: Library,
        journal: Journal,
        scanner: Scanner,
        worker: Worker,
        cloud: Cloud,
        ark: ArkApi,
        clock: Clock,
    ) -> None:
        self.conn = conn
        self.lib = library
        self.journal = journal
        self.scanner = scanner
        self.worker = worker
        self.cloud = cloud
        self.ark = ark
        self.clock = clock
        self.current: asyncio.Task[GlobalError | None] | None = None
        self.current_id: int | None = None
        self.storage_fault = False
        self.last_activity = clock.now_ms()
        self._cancel_reason: str | None = None
        self._start_attempts = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._executor_signal = asyncio.Event()
        self._scan_signal = asyncio.Event()
        self._loops: list[asyncio.Task[None]] = []

    # ---- 启动与关闭 ----

    def start(self) -> None:
        self._loops = [
            asyncio.create_task(self._loop(self.scan_tick, self._scan_interval, self._scan_signal)),
            asyncio.create_task(self._executor_loop()),
            asyncio.create_task(self._loop(self.cloud_tick, lambda: CLOUD_INTERVAL_S, asyncio.Event())),
            asyncio.create_task(self._loop(self.network_tick, lambda: NETWORK_INTERVAL_S, asyncio.Event())),
        ]

    async def shutdown(self) -> None:
        """取消后台循环；正在处理的任务按“中断”处理，下次启动退回排队最前（13.4 第 5 步）。"""
        self.worker.shutting_down = True
        for task in self._loops:
            task.cancel()
        if self.current is not None:
            self.current.cancel()
        await asyncio.gather(*self._loops, *([self.current] if self.current else []), return_exceptions=True)

    # ---- 队列控制 ----

    def state(self) -> sqlite3.Row:
        return repo.get_runtime_state(self.conn)

    def pause(self) -> None:
        change = status.on_user_queue_action(self.state()["queue_state"], "pause")
        self._apply(change)

    async def resume(self) -> None:
        """继续：从出错继续时先重新执行启动第 2–5 步（检查视频库、恢复记录）。"""
        current = self.state()["queue_state"]
        change = status.on_user_queue_action(current, "resume")
        if current == "error":
            error = await startup.init_library(self.conn, self.lib, self.journal, self.clock)
            if error is not None:
                self.storage_fault = error.storage
                raise Conflict(str(error))
            self.storage_fault = False
        self._apply(change)

    def wake(self) -> None:
        self._apply(status.on_user_queue_action(self.state()["queue_state"], "wake"))

    def wake_if_sleeping(self) -> None:
        change = status.wake_if_sleeping(self.state()["queue_state"])
        if change is not None:
            self._apply(change)

    def notify_enqueued(self) -> None:
        self.last_activity = self.clock.now_ms()
        self._executor_signal.set()

    def enter_error(self, exc: GlobalError) -> None:
        """处理之外遇到影响所有任务的错误：队列进入出错，正在处理的任务退回排队最前（立项 8.1）。"""
        state = self.state()
        change = status.on_global_error(state["queue_state"], bool(state["error_auto_resume"]),
                                        str(exc), exc.auto_resume)
        repo.set_queue_state(self.conn, change.state, change.error_message, change.auto_resume, False,
                             self.clock.now_ms())
        self.storage_fault = exc.storage
        log.error("队列进入出错：%s", exc)
        if self.current is not None:
            self._cancel_reason = "global"
            self.current.cancel()

    async def stop_task(self, task_id: int) -> bool:
        """停止正在处理的任务：杀掉 ffmpeg，删除云端副本和临时文件，改为已停止。不是当前任务返回 False。"""
        if self.current is None or self.current_id != task_id:
            return False
        task = repo.get_task(self.conn, task_id)
        if task is not None and task["step"] == "commit":
            raise Conflict("正在保存结果，不能停止")
        self._cancel_reason = "stop"
        self.current.cancel()
        await self._idle.wait()
        return True

    def _apply(self, change: status.QueueChange) -> None:
        repo.set_queue_state(self.conn, change.state, change.error_message, change.auto_resume,
                             change.reset_failures, self.clock.now_ms())
        if change.state == "running":
            self.last_activity = self.clock.now_ms()
            self._executor_signal.set()
            self._scan_signal.set()

    # ---- tick ----

    async def executor_tick(self) -> bool:
        """队列运行中且有排队任务时处理一个；返回是否处理了任务。"""
        if self.state()["queue_state"] != "running" or self.current is not None:
            return False
        task = repo.next_queued(self.conn)
        if task is None:
            return False
        self.current_id, self._start_attempts, self._cancel_reason = task["id"], task["attempts"], None
        self._idle.clear()
        self.last_activity = self.clock.now_ms()
        self.current = asyncio.create_task(self.worker.process(task["id"]))
        try:
            error = await self.current
            if error is not None:
                self.storage_fault = error.storage
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self._cancel_reason is None or (current is not None and current.cancelling()):
                raise  # 关机
            self._after_cancel(task["id"], self._cancel_reason)
        finally:
            self.current, self.current_id = None, None
            self.last_activity = self.clock.now_ms()
            self._idle.set()
        return True

    async def scan_tick(self) -> None:
        if self.storage_fault:
            return
        sleeping = self.state()["queue_state"] == "sleeping"
        try:
            result = await self.scanner.scan()
        except GlobalError as exc:
            self.enter_error(exc)
            return
        if result.new_files or result.pending:
            self.last_activity = self.clock.now_ms()
        if result.enqueued:
            self._executor_signal.set()
        if sleeping and result.new_files:
            self.wake_if_sleeping()
        await self.scanner.housekeeping()
        if sleeping:
            # 立项第 5 节：休眠时清理也暂停，等这次扫描时一起做
            await self.cloud.cleanup(repo.get_settings(self.conn)["api_key"])
        self.maybe_sleep()

    async def cloud_tick(self) -> None:
        state = self.state()
        if state["queue_state"] == "sleeping" or (state["queue_state"] == "error" and state["error_auto_resume"]):
            return
        await self.cloud.cleanup(repo.get_settings(self.conn)["api_key"])

    async def network_tick(self) -> None:
        state = self.state()
        if state["queue_state"] != "error" or not state["error_auto_resume"]:
            return
        if await self.ark.ping():
            change = status.on_network_recovered(state["queue_state"], True)
            if change is not None:
                log.info("网络已恢复，队列自动继续")
                self._apply(change)

    def maybe_sleep(self) -> None:
        settings = repo.get_settings(self.conn)
        if status.should_sleep(
            self.state()["queue_state"],
            bool(settings["sleep_enabled"]),
            self.clock.now_ms() - self.last_activity,
            settings["sleep_idle_minutes"],
            has_queued=repo.next_queued(self.conn) is not None,
            has_processing=self.current is not None,
            has_pending_files=self.scanner.has_pending(),
        ):
            repo.set_queue_state(self.conn, "sleeping", None, False, False, self.clock.now_ms())
            log.info("进入休眠")

    # ---- 循环 ----

    def _scan_interval(self) -> float:
        if self.state()["queue_state"] == "sleeping":
            return repo.get_settings(self.conn)["sleep_interval_hours"] * 3600
        return SCAN_INTERVAL_S

    async def _loop(self, tick: Callable[[], Awaitable[None]], interval: Callable[[], float],
                    signal: asyncio.Event) -> None:
        while True:
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("后台循环出错")  # 只在循环最外层兜底（技术选型 13.3）
            await self.clock.wait(signal, interval())
            signal.clear()

    async def _executor_loop(self) -> None:
        while True:
            try:
                if await self.executor_tick():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("执行器出错")
            await self.clock.wait(self._executor_signal, EXECUTOR_IDLE_S)
            self._executor_signal.clear()

    def _after_cancel(self, task_id: int, reason: str) -> None:
        task = repo.get_task(self.conn, task_id)
        if task is None or task["status"] != "processing" or repo.get_op(self.conn, task_id) is not None:
            return
        now = self.clock.now_ms()
        if reason == "stop":
            repo.mark_stopped(self.conn, task_id, now)
            tasklog.write_task(self.conn, now, task_id, "info", "已停止")
        else:
            repo.requeue_front(self.conn, task_id, self._start_attempts, now)
            tasklog.write_task(self.conn, now, task_id, "warning", "队列出错，任务退回排队最前")
