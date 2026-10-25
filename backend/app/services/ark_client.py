"""方舟接口（立项附录 B，技术选型第 11 节）。直接调 REST 接口，不使用 SDK，也不自动重试。

HTTP 错误按 domain.errors.classify_ark 分类；连不上、超时统一抛 NetworkError；
删除时方舟返回“文件不存在”视为已删除。Key 只放在请求头里，不写进任何日志。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

import httpx2

from app.domain.errors import (
    ATTEMPT_TIME_LIMIT, OUTPUT_TOO_LONG, NetworkError, Retryable, TaskFailed, classify_ark, is_already_deleted,
)
from app.domain.audio import AnalysisMode
from app.domain.prompt import text_format

BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
MODEL_ID = "doubao-seed-2-0-lite-260428"  # 2026-10-24 由 2.1 Lite 换成 2.0 Lite（技术选型 11.7）
MAX_OUTPUT_TOKENS = 65536  # 约够 4 小时连续讲话的转写（立项附录 B）
# 文件过期时间 = 发请求时 + 1 天 + 1 小时（方舟要求严格大于 1 天）；回答比文件早 1 小时过期（立项附录 B）
FILE_TTL_S = 86400 + 3600
ANSWER_EARLIER_S = 3600
_CHUNK = 1024 * 1024
_TIMEOUT = httpx2.Timeout(connect=15, read=120, write=120, pool=15)
# 分析时模型可能要生成很久才开始返回，读取超时放宽到单次尝试的时限，由 worker 的 60 分钟时限兜底
_ANALYZE_TIMEOUT = httpx2.Timeout(connect=15, read=ATTEMPT_TIME_LIMIT, write=120, pool=15)


class ArkApi(Protocol):
    async def upload(self, api_key: str, path: Path, expire_at: int) -> str:
        """上传视频，返回文件 ID。expire_at 为 Unix 秒。"""
        ...

    async def file_status(self, api_key: str, file_id: str) -> str:
        """返回 processing / active / failed。"""
        ...

    async def analyze(
        self, api_key: str, file_id: str, prompt: str, expire_at: int, kind: AnalysisMode = "video"
    ) -> str:
        """返回模型输出的文字（应为 JSON）。kind：video 或 audio（超过 512MB 只传音频）。"""
        ...

    async def delete(self, api_key: str, file_id: str) -> None: ...

    async def ping(self) -> bool:
        """不带 Key 请求方舟地址；收到任何 HTTP 响应就算网络通。"""
        ...


class ArkClient:
    def __init__(self, client: httpx2.AsyncClient) -> None:
        self.client = client

    @classmethod
    def create(cls, transport: httpx2.AsyncBaseTransport | None = None) -> "ArkClient":
        """启动时建一个共用的 AsyncClient，关闭时调用 aclose()（技术选型 13.4）。"""
        return cls(httpx2.AsyncClient(base_url=BASE_URL, timeout=_TIMEOUT, transport=transport))

    async def aclose(self) -> None:
        await self.client.aclose()

    async def upload(self, api_key: str, path: Path, expire_at: int) -> str:
        """multipart 请求体自己拼：分块在线程里读文件，不一次读进内存（技术选型 13.15）。

        .m4a 是只传音频时提取的音轨：不填视频预处理（技术选型 11.6 实测）。
        """
        boundary = uuid.uuid4().hex
        suffix = path.suffix.lower()
        fields = {"purpose": "user_data", "expire_at": str(expire_at)}
        if suffix != ".m4a":
            fields["preprocess_configs[video][model]"] = MODEL_ID  # 必须填模型 ID，否则模型听不到声音（立项附录 B）
        head = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in fields.items()
        )
        media_type = {".mov": "video/quicktime", ".m4a": "audio/x-m4a"}.get(suffix, "video/mp4")
        head += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="video{suffix}"\r\n'
                 f"Content-Type: {media_type}\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        size = (await asyncio.to_thread(path.stat)).st_size
        headers = {**_auth(api_key), "Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Content-Length": str(len(head) + size + len(tail))}
        data = await self._request("POST", "/files", headers=headers, content=_body(head, path, tail))
        return data["id"]

    async def file_status(self, api_key: str, file_id: str) -> str:
        """预处理失败时直接抛错：文件本身有问题（InvalidParameter）不重试，其他原因可重试。"""
        data = await self._request("GET", f"/files/{file_id}", headers=_auth(api_key))
        if data["status"] == "failed":
            error = data.get("error") or {}
            # 阶段 4 实测：不是视频的文件 → status=failed，error.code=InvalidParameter，“Invalid video_url.”
            if error.get("code") == "InvalidParameter":
                raise TaskFailed(f"方舟无法处理这个视频：{error.get('message')}")
            raise Retryable(f"视频预处理失败：{error.get('code')} {error.get('message')}")
        return data["status"]

    async def analyze(
        self, api_key: str, file_id: str, prompt: str, expire_at: int, kind: AnalysisMode = "video"
    ) -> str:
        body = {
            "model": MODEL_ID,
            "input": [{"role": "user", "content": [
                {"type": f"input_{kind}", "file_id": file_id},
                {"type": "input_text", "text": prompt},
            ]}],
            "store": False,
            "expire_at": expire_at,  # 要比视频的过期时间早
            "thinking": {"type": "disabled"},
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "text": {"format": text_format()},  # 扁平写法（阶段 0 实测：嵌套写法会被拒绝）
        }
        data = await self._request("POST", "/responses", headers=_auth(api_key), json=body,
                                   timeout=_ANALYZE_TIMEOUT)
        return output_text(data)

    async def delete(self, api_key: str, file_id: str) -> None:
        await self._request("DELETE", f"/files/{file_id}", headers=_auth(api_key), missing_ok=True)

    async def ping(self) -> bool:
        try:
            await self.client.get("", timeout=10)
        except httpx2.TransportError:
            return False
        return True

    async def _request(self, method: str, url: str, missing_ok: bool = False, **kwargs: Any) -> dict[str, Any]:
        """missing_ok：方舟返回“文件不存在”时当作成功（删除用）。"""
        try:
            resp = await self.client.request(method, url, **kwargs)
        except httpx2.TransportError as exc:
            raise NetworkError(f"无法连接方舟：{type(exc).__name__}") from None
        if resp.is_success:
            try:
                return resp.json()
            except ValueError:
                raise Retryable(f"方舟返回的不是 JSON（HTTP {resp.status_code}）") from None
        try:
            error = resp.json().get("error") or {}
        except (ValueError, AttributeError):
            error = {}
        code, message, param = error.get("code"), error.get("message"), error.get("param")
        if missing_ok and is_already_deleted(resp.status_code, code):
            return {}
        raise classify_ark(resp.status_code, code, message, param, resp.headers.get("retry-after"))


def output_text(data: dict[str, Any]) -> str:
    """从 Responses API 的返回里取出模型的文字输出。"""
    if data.get("status") == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason")
        if reason == "length":
            # 阶段 4 实测：输出达到 max_output_tokens 时 HTTP 200、status=incomplete、reason=length
            raise TaskFailed(OUTPUT_TOO_LONG)
        raise Retryable(f"方舟返回的结果不完整：{reason}")  # 其他原因没实测，按可重试
    parts = [
        content.get("text", "")
        for item in data.get("output") or [] if item.get("type") == "message"
        for content in item.get("content") or [] if content.get("type") == "output_text"
    ]
    if not parts:
        raise Retryable("方舟没有返回文字结果")
    return "".join(parts)


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def _body(head: bytes, path: Path, tail: bytes) -> AsyncIterator[bytes]:
    yield head
    f = await asyncio.to_thread(open, path, "rb")
    try:
        while chunk := await asyncio.to_thread(f.read, _CHUNK):
            yield chunk
    finally:
        await asyncio.to_thread(f.close)
    yield tail
