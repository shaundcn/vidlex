"""云端副本的登记、删除、过期丢弃（立项第 3 节第 7 步，技术选型 13.9）。

上传后立刻登记（崩溃后也能清理），同时记为“正在使用”；后台清理跳过正在使用的副本，
任务或测试用完调用 delete 时解除。只在内存里记，重启后没有正在使用的副本。
"""

import logging
import sqlite3

from app.db import repo
from app.domain.errors import NetworkError, TaskError
from app.services.ark_client import ArkApi
from app.services.clock import Clock

log = logging.getLogger("vidlex.cloud")


class Cloud:
    def __init__(self, conn: sqlite3.Connection, ark: ArkApi, clock: Clock) -> None:
        self.conn = conn
        self.ark = ark
        self.clock = clock
        self.in_use: set[str] = set()

    def register(self, file_id: str, expire_at_s: int) -> None:
        """上传成功拿到 ID 立刻登记，并记为正在使用。"""
        repo.add_cloud_file(self.conn, file_id, expire_at_s * 1000, self.clock.now_ms())
        self.in_use.add(file_id)

    async def delete(self, api_key: str | None, file_id: str) -> bool:
        """用完后删除一个云端副本；失败时保留登记，留给后台清理。"""
        self.in_use.discard(file_id)
        if not api_key:
            return False
        try:
            await self.ark.delete(api_key, file_id)
        except TaskError as exc:
            log.warning("删除云端副本失败，稍后重试：%s（%s）", file_id, exc)
            return False
        repo.remove_cloud_file(self.conn, file_id)
        return True

    async def cleanup(self, api_key: str | None) -> None:
        """逐个删除登记的副本；已过期的直接注销（换了 Key 后删不掉的旧副本也靠这条停止重试）。"""
        now = self.clock.now_ms()
        for row in repo.list_cloud_files(self.conn):
            if row["file_id"] in self.in_use:
                continue  # 任务或测试还在用：2026-10-24 NAS 上任务 32 的副本在分析前被清理删掉
            if row["expire_at"] <= now:
                repo.remove_cloud_file(self.conn, row["file_id"])
                continue
            if not api_key:
                return
            try:
                await self.ark.delete(api_key, row["file_id"])
            except NetworkError:
                return  # 断网：这一轮跳过
            except TaskError as exc:
                log.warning("删除云端副本失败，稍后重试：%s（%s）", row["file_id"], exc)
                continue
            repo.remove_cloud_file(self.conn, row["file_id"])
