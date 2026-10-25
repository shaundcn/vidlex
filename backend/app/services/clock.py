"""可替换的时钟（技术选型 13.3）：业务代码从这里取时间、等待和限时，测试时换成假时钟。"""

import asyncio
import time
from contextlib import AbstractAsyncContextManager


class Clock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def limit(self, seconds: float) -> AbstractAsyncContextManager[object]:
        """限时：超时后在块的出口抛 TimeoutError。"""
        return asyncio.timeout(seconds)

    async def wait(self, event: asyncio.Event, seconds: float) -> bool:
        """等到事件被设置或超时；返回事件是否已设置。"""
        try:
            async with asyncio.timeout(seconds):
                await event.wait()
        except TimeoutError:
            pass
        return event.is_set()
