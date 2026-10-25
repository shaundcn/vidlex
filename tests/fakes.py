"""假时钟和假方舟，给 worker、runtime 等的测试用（正式客户端 ark_client 的请求格式用 httpx2.MockTransport 测）。"""

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

RESULT = {
    "title": "3 步用 PS 快速抠出发丝",
    "main_content": {"overview": "简述", "sections": ["第一段", "第二段"], "conclusion": "结论"},
    "highlight": "亮点",
    "transcript": ["大家好。"],
}


class FakeClock:
    """虚拟时间：sleep 立即返回并推进时间；超过 limit 设定的期限时抛 TimeoutError。"""

    def __init__(self, start_ms: int = 1_800_000_000_000) -> None:
        self.ms = start_ms
        self.slept: list[float] = []
        self._deadlines: list[int] = []

    def now_ms(self) -> int:
        return self.ms

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        target = self.ms + int(seconds * 1000)
        if self._deadlines and target > min(self._deadlines):
            self.ms = max(self.ms, min(self._deadlines))
            await asyncio.sleep(0)
            raise TimeoutError
        self.ms = target
        await asyncio.sleep(0)

    @asynccontextmanager
    async def limit(self, seconds: float) -> AsyncIterator[None]:
        deadline = self.ms + int(seconds * 1000)
        self._deadlines.append(deadline)
        try:
            yield
        finally:
            self._deadlines.remove(deadline)

    async def wait(self, event: asyncio.Event, seconds: float) -> bool:
        if not event.is_set():
            await self.sleep(seconds)
        return event.is_set()

    def advance(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)


class FakeArk:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.files: dict[str, dict] = {}
        self.uploads: list[Path] = []
        self.upload_expire: list[int] = []
        self.analyze_expire: list[int] = []
        self.prompts: list[str] = []
        self.kinds: list[str] = []  # 每次分析传的是 video 还是 audio
        self.deleted: list[str] = []
        self.errors: dict[str, list[BaseException]] = defaultdict(list)
        self.results: list[str] = []
        self.analyze_seconds: list[float] = []
        self.polls_before_active: list[int] = []
        self.on_analyze = None  # 可选的异步回调：分析期间做点什么（删除源视频、等停止）
        self.online = True
        self._n = 0

    def fail(self, method: str, *errors: BaseException) -> None:
        self.errors[method].extend(errors)

    def _check(self, method: str) -> None:
        if self.errors[method]:
            raise self.errors[method].pop(0)

    async def upload(self, api_key: str, path: Path, expire_at: int) -> str:
        self._check("upload")
        self._n += 1
        file_id = f"file-{self._n}"
        self.uploads.append(path)
        self.upload_expire.append(expire_at)
        polls = self.polls_before_active.pop(0) if self.polls_before_active else 0
        self.files[file_id] = {"polls": polls, "size": path.stat().st_size}
        return file_id

    async def file_status(self, api_key: str, file_id: str) -> str:
        self._check("file_status")
        info = self.files[file_id]
        if info["polls"] > 0:
            info["polls"] -= 1
            return "processing"
        return "active"

    async def analyze(self, api_key: str, file_id: str, prompt: str, expire_at: int, kind: str = "video") -> str:
        self._check("analyze")
        self.prompts.append(prompt)
        self.kinds.append(kind)
        self.analyze_expire.append(expire_at)
        if self.on_analyze is not None:
            await self.on_analyze()
        await self.clock.sleep(self.analyze_seconds.pop(0) if self.analyze_seconds else 10)
        return self.results.pop(0) if self.results else json.dumps(RESULT, ensure_ascii=False)

    async def delete(self, api_key: str, file_id: str) -> None:
        self._check("delete")
        self.files.pop(file_id, None)
        self.deleted.append(file_id)

    async def ping(self) -> bool:
        return self.online
