"""用户操作：任务的开始、停止、忽略，队列的暂停、继续、唤醒，保存设置（立项 4.1、4.3、第 5 节，技术选型 13.9）。

所有任务操作先用 domain.status 判断当前状态能不能做，不能做抛 Conflict（409）。
"""

import sqlite3
from collections.abc import Awaitable, Callable

from app.db import repo
from app.domain import status
from app.domain.errors import Conflict, GlobalError, NotFound
from app.domain.prompt import DEFAULT_PROMPTS
from app.logging_setup import set_api_key
from app.services import tasklog
from app.services.clock import Clock
from app.services.journal import Journal
from app.services.runtime import Runtime


class Actions:
    def __init__(self, conn: sqlite3.Connection, journal: Journal, runtime: Runtime, clock: Clock) -> None:
        self.conn = conn
        self.journal = journal
        self.runtime = runtime
        self.clock = clock

    # ---- 任务 ----

    async def start(self, task_id: int) -> sqlite3.Row:
        effect = self._effect(task_id, "start")
        if effect == "enqueue":
            repo.requeue_tail(self.conn, task_id, self.clock.now_ms())
            self._log(task_id, "重新排队")
        elif effect == "redo":
            await self._journal(self.journal.redo, task_id)
        else:
            await self._journal(self.journal.unignore, task_id)
        # 在任务上点“开始”时，队列正在休眠也自动唤醒（立项 4.1）
        self.runtime.wake_if_sleeping()
        self.runtime.notify_enqueued()
        return self._task(task_id)

    async def stop(self, task_id: int) -> sqlite3.Row:
        if self._effect(task_id, "stop") == "stop_queued":
            repo.mark_stopped(self.conn, task_id, self.clock.now_ms())
            self._log(task_id, "已停止")
        elif not await self.runtime.stop_task(task_id):
            raise Conflict("任务没有在处理，请刷新后再试")
        return self._task(task_id)

    async def ignore(self, task_id: int) -> sqlite3.Row:
        self._effect(task_id, "ignore")
        return await self._journal(self.journal.ignore, task_id)

    # ---- 队列 ----

    def pause(self) -> None:
        self.runtime.pause()

    async def resume(self) -> None:
        await self.runtime.resume()

    def wake(self) -> None:
        self.runtime.wake()

    # ---- 设置（取值校验在接口层的 Pydantic 模型里）----

    @staticmethod
    def default_prompts() -> dict[str, str]:
        """“恢复默认”用；默认提示词只在 domain.prompt 里保存一份。"""
        return dict(DEFAULT_PROMPTS)

    def save_api_key(self, key: str) -> None:
        repo.save_api_key(self.conn, key, self.clock.now_ms())
        set_api_key(key)  # 日志过滤器立即用新 Key

    def save_prompts(self, prompts: dict[str, str]) -> None:
        repo.save_prompts(self.conn, prompts, self.clock.now_ms())

    def save_sleep(self, enabled: bool, idle_minutes: int, interval_hours: int) -> None:
        repo.save_sleep(self.conn, enabled, idle_minutes, interval_hours, self.clock.now_ms())
        if not enabled:
            self.runtime.wake_if_sleeping()  # 关闭休眠：立即回到运行中

    # ---- 内部 ----

    def _effect(self, task_id: int, action: str) -> str:
        task = self._task(task_id)
        return status.action_effect(
            task["status"], task["step"], repo.get_op(self.conn, task_id) is not None,
            task["current_video_path"] is not None, action,
        )

    async def _journal(self, operation: Callable[[int], Awaitable[sqlite3.Row]], task_id: int) -> sqlite3.Row:
        """剪切过程中遇到影响所有任务的错误：队列进入出错，返回 409 并写明原因。"""
        try:
            return await operation(task_id)
        except GlobalError as exc:
            self.runtime.enter_error(exc)
            raise Conflict(str(exc)) from exc

    def _task(self, task_id: int) -> sqlite3.Row:
        task = repo.get_task(self.conn, task_id)
        if task is None:
            raise NotFound("任务不存在")
        return task

    def _log(self, task_id: int, message: str) -> None:
        """用户操作写进任务日志（技术选型 13.13）；走 journal 和 runtime 的操作由它们自己写。"""
        tasklog.write_task(self.conn, self.clock.now_ms(), task_id, "info", message)
