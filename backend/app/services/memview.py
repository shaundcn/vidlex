"""内存视图（技术选型 4.11、12.6、13.4）：摘要接口和“没有新日志”的日志接口只读这里。

由数据库连接的“提交后回调”刷新：每次写事务提交后立即刷新，轮询请求不查数据库，
vidlex 休眠时页面开着也不会唤醒硬盘。
"""

import sqlite3
import time

from app.db import repo

COUNTED = ("queued", "completed", "failed", "stopped", "ignored")


class MemView:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # 起始值用启动时间：重启后不会和页面上记着的旧版本号相同（只在内存里，每次启动都重新计数）
        self.list_version = time.time_ns() // 1_000_000
        self.counts: dict[str, int] = {}
        self.refresh(True)
        conn.commit_listeners.append(self.refresh)  # type: ignore[attr-defined]

    def refresh(self, tasks_changed: bool) -> None:
        if tasks_changed:
            self.list_version += 1
            counts = repo.count_by_status(self.conn)
            self.counts = {status: counts.get(status, 0) for status in COUNTED}
        self.max_log_id = repo.max_log_id(self.conn)
        state = repo.get_runtime_state(self.conn)
        self.queue_state: str = state["queue_state"]
        self.error_message: str | None = state["error_message"]
        self.settings = repo.get_settings(self.conn)
