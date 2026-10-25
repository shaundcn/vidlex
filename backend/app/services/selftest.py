"""设置页的“测试”（立项第 5 节，技术选型 13.9）。

用已保存的 Key 处理内置的几秒短视频，流程和正式任务相同：上传、等待 active、分析、校验返回格式，
云端副本照样登记并删除。同一时间只能跑一个，最长 110 秒，不影响队列。

内置视频 assets/test.mp4 在开发环境生成一次、随代码提交（镜像里不需要 flite）：
    ffmpeg -f lavfi -i "flite=text='This is the vidlex self test. The secret word is giraffe. Giraffe.':voice=slt" \\
           -f lavfi -i "color=c=0x1a73e8:s=320x240:r=10" -shortest -c:v libx264 -pix_fmt yuv420p \\
           -c:a aac -b:a 64k -movflags +faststart assets/test.mp4
"""

import asyncio
import sqlite3
from pathlib import Path

from app.db import repo
from app.domain.errors import (
    KEY_INVALID, MODEL_UNAVAILABLE, Conflict, GlobalError, NetworkError, RateLimited, Retryable, TaskError,
)
from app.domain.prompt import build_prompt
from app.domain.result import parse_result
from app.services.ark_client import ANSWER_EARLIER_S, FILE_TTL_S, ArkApi
from app.services.clock import Clock
from app.services.cloud import Cloud

ASSET = Path(__file__).resolve().parent.parent / "assets" / "test.mp4"
KEYWORD = "giraffe"
TIME_LIMIT_S = 110
POLL_INTERVAL_S = 2
CLEANUP_LIMIT_S = 10


class SelfTest:
    def __init__(self, conn: sqlite3.Connection, ark: ArkApi, cloud: Cloud, clock: Clock) -> None:
        self.conn = conn
        self.ark = ark
        self.cloud = cloud
        self.clock = clock
        self._lock = asyncio.Lock()

    async def run(self) -> tuple[bool, str]:
        """返回 (是否成功, 给用户看的说明)。已有测试在跑时抛 Conflict（409）。"""
        if self._lock.locked():
            raise Conflict("测试正在进行，请稍候")
        async with self._lock:
            settings = repo.get_settings(self.conn)
            api_key = settings["api_key"]
            if not api_key:
                return False, "请先保存 API Key"
            prompt = build_prompt(repo.prompts_of(settings))
            file_id: str | None = None
            try:
                async with self.clock.limit(TIME_LIMIT_S):
                    file_expire = self.clock.now_ms() // 1000 + FILE_TTL_S
                    file_id = await self.ark.upload(api_key, ASSET, file_expire)
                    self.cloud.register(file_id, file_expire)
                    while (state := await self.ark.file_status(api_key, file_id)) != "active":
                        if state == "failed":
                            return False, "测试失败：方舟预处理视频失败"
                        await self.clock.sleep(POLL_INTERVAL_S)
                    text = await self.ark.analyze(api_key, file_id, prompt, file_expire - ANSWER_EARLIER_S)
            except TimeoutError:
                return False, f"测试超时（{TIME_LIMIT_S} 秒），请稍后再试"
            except TaskError as exc:
                return False, _message(exc)
            finally:
                if file_id is not None:
                    await self._delete(api_key, file_id)
            try:
                result = parse_result(text)
            except Retryable:
                return False, "返回格式不合格"
            if KEYWORD not in " ".join(result.transcript).lower():
                return False, "模型没有听出声音"
            return True, "测试成功"

    async def _delete(self, api_key: str, file_id: str) -> None:
        """删除失败时副本留在登记表里，由后台清理；最多等 10 秒，不拖长测试。"""
        try:
            async with self.clock.limit(CLEANUP_LIMIT_S):
                await self.cloud.delete(api_key, file_id)
        except TimeoutError:
            pass

def _message(exc: TaskError) -> str:
    if isinstance(exc, GlobalError) and str(exc) == KEY_INVALID:
        return "API Key 无效"
    if isinstance(exc, GlobalError) and str(exc) == MODEL_UNAVAILABLE:
        return "Seed 2.0 Lite 未开通"
    if isinstance(exc, NetworkError):
        return "无法连接方舟"
    if isinstance(exc, RateLimited):
        return "方舟限流，请稍后再试"
    return f"测试失败：{exc}"
