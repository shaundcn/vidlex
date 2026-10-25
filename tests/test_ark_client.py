"""正式客户端（立项附录 B，技术选型 11.3、13.10）：用 httpx2.MockTransport 模拟方舟。"""

import json
from pathlib import Path

import httpx2
import pytest

from app.domain.errors import (
    CLOCK_WRONG, KEY_INVALID, MODEL_UNAVAILABLE, GlobalError, NetworkError, RateLimited, Retryable,
    TaskFailed,
)
from app.services.ark_client import MAX_OUTPUT_TOKENS, MODEL_ID, ArkClient, output_text

pytestmark = pytest.mark.anyio
KEY = "ark-secret-key-0000"


def client(handler) -> ArkClient:
    return ArkClient.create(transport=httpx2.MockTransport(handler))


def error(status: int, code: str, param: str | None = None, headers=None) -> httpx2.Response:
    body = {"error": {"code": code, "message": f"{code} 的说明", "param": param, "type": "x"}}
    return httpx2.Response(status, json=body, headers=headers)


async def test_upload_request_format(tmp_path: Path) -> None:
    video = tmp_path / "a.MOV"
    video.write_bytes(b"x" * (3 * 1024 * 1024 + 7))  # 跨过 1MB 分块
    seen = {}

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"], seen["headers"] = str(request.url), request.headers
        seen["body"] = await request.aread()
        return httpx2.Response(200, json={"id": "file-abc", "status": "processing"})

    assert await client(handler).upload(KEY, video, 1_900_000_000) == "file-abc"
    assert seen["url"] == "https://ark.cn-beijing.volces.com/api/v3/files"
    assert seen["headers"]["authorization"] == f"Bearer {KEY}"
    assert int(seen["headers"]["content-length"]) == len(seen["body"])  # 不用分块传输编码
    boundary = seen["headers"]["content-type"].split("boundary=")[1]
    body = seen["body"]
    for name, value in (("purpose", "user_data"), ("expire_at", "1900000000"),
                        ("preprocess_configs[video][model]", MODEL_ID)):
        assert f'name="{name}"\r\n\r\n{value}\r\n'.encode() in body
    assert b'filename="video.mov"\r\nContent-Type: video/quicktime\r\n\r\n' + b"x" * 100 in body
    assert body.endswith(b"x" * 7 + f"\r\n--{boundary}--\r\n".encode())


async def test_upload_audio_only(tmp_path: Path) -> None:
    """只传音频的 .m4a：不填视频预处理（技术选型 11.6 实测）。"""
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"a" * 10)
    seen = {}

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = await request.aread()
        return httpx2.Response(200, json={"id": "file-a"})

    await client(handler).upload(KEY, audio, 1)
    assert b"preprocess_configs" not in seen["body"]
    assert b'filename="video.m4a"\r\nContent-Type: audio/x-m4a\r\n' in seen["body"]


async def test_upload_streams_in_chunks(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "a.mp4"
    video.write_bytes(b"y" * (2 * 1024 * 1024 + 1))
    reads = []
    original = open

    def spy_open(*args, **kwargs):
        f = original(*args, **kwargs)
        read = f.read

        def counted(n=-1):
            reads.append(n)
            return read(n)

        f.read = counted  # type: ignore[method-assign]
        return f

    monkeypatch.setattr("builtins.open", spy_open)

    async def handler(request):
        await request.aread()
        return httpx2.Response(200, json={"id": "f"})

    await client(handler).upload(KEY, video, 1)
    assert reads and all(n == 1024 * 1024 for n in reads)  # 每次只读 1MB，不一次读进内存


async def test_analyze_request_format() -> None:
    seen = {}
    reply = {"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": '{"title": "t"'},
                                        {"type": "output_text", "text": "}"}]}]}

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx2.Response(200, json=reply)

    text = await client(handler).analyze(KEY, "file-abc", "title：x", 1_800_000_000)
    assert text == '{"title": "t"}'
    body = seen["body"]
    assert seen["url"].endswith("/responses")
    assert body["model"] == MODEL_ID and body["store"] is False and body["expire_at"] == 1_800_000_000
    assert body["thinking"] == {"type": "disabled"} and body["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert body["input"][0]["content"] == [{"type": "input_video", "file_id": "file-abc"},
                                           {"type": "input_text", "text": "title：x"}]
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and "json_schema" not in fmt  # 扁平写法
    await client(handler).analyze(KEY, "file-abc", "p", 1, kind="audio")
    assert seen["body"]["input"][0]["content"][0] == {"type": "input_audio", "file_id": "file-abc"}


async def test_status_and_delete() -> None:
    calls = []

    async def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx2.Response(200, json={"id": "f", "status": "active"})
        if "gone" in request.url.path:
            return error(404, "ResourceNotFound")
        return httpx2.Response(200, json={"deleted": True})

    c = client(handler)
    assert await c.file_status(KEY, "f") == "active"
    await c.delete(KEY, "f")
    await c.delete(KEY, "gone")  # “文件不存在”视为已删除
    assert calls == [("GET", "/api/v3/files/f"), ("DELETE", "/api/v3/files/f"), ("DELETE", "/api/v3/files/gone")]


@pytest.mark.parametrize("response, kind, message", [
    (error(401, "AuthenticationError"), GlobalError, KEY_INVALID),
    (error(404, "InvalidEndpointOrModel.NotFound"), GlobalError, MODEL_UNAVAILABLE),
    (error(400, "InvalidParameter", "expire_at"), GlobalError, CLOCK_WRONG),
    (error(400, "InvalidParameter", "file_id"), TaskFailed, "程序参数错误（file_id）"),
    (error(503, "ServiceUnavailable"), Retryable, "方舟服务端错误 503"),
    (error(403, "SomethingNew"), Retryable, "SomethingNew"),
    (httpx2.Response(502, text="<html>bad gateway</html>"), Retryable, "502"),
])
async def test_error_mapping(response, kind, message: str) -> None:
    async def handler(request):
        return response

    with pytest.raises(kind) as info:
        await client(handler).file_status(KEY, "f")
    assert type(info.value) is kind and message in str(info.value)
    assert KEY not in str(info.value)


async def test_rate_limit_retry_after() -> None:
    async def handler(request):
        return error(429, "RateLimitExceeded", headers={"Retry-After": "17"})

    with pytest.raises(RateLimited) as info:
        await client(handler).file_status(KEY, "f")
    assert info.value.retry_after == 17


async def test_delete_other_errors_raise() -> None:
    async def handler(request):
        return error(401, "AuthenticationError")

    with pytest.raises(GlobalError):
        await client(handler).delete(KEY, "f")


@pytest.mark.parametrize("exc", [httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow")])
async def test_network_errors(exc) -> None:
    async def handler(request):
        raise exc

    c = client(handler)
    with pytest.raises(NetworkError) as info:
        await c.file_status(KEY, "f")
    assert KEY not in str(info.value)
    assert await c.ping() is False


async def test_ping_any_http_response_counts() -> None:
    async def handler(request):
        assert "authorization" not in request.headers  # 不带 Key
        return httpx2.Response(404)

    assert await client(handler).ping() is True


def test_output_text_problems() -> None:
    # 阶段 4 实测的截断格式：直接失败“内容过长”，不重试
    with pytest.raises(TaskFailed, match=r"^内容过长$"):
        output_text({"status": "incomplete", "incomplete_details": {"reason": "length"},
                     "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ti'}]}]})
    with pytest.raises(Retryable, match="不完整"):
        output_text({"status": "incomplete", "incomplete_details": {"reason": "没见过的原因"}})
    with pytest.raises(Retryable, match="没有返回文字"):
        output_text({"status": "completed", "output": []})


@pytest.mark.parametrize("error_body, kind, message", [
    ({"code": "InvalidParameter", "message": 'InvalidArgumentError("Invalid video_url.")', "param": "",
      "type": "BadRequest"}, TaskFailed, "方舟无法处理这个视频"),
    ({"code": "SomethingElse", "message": "x"}, Retryable, "视频预处理失败"),
])
async def test_preprocess_failed(error_body, kind, message: str) -> None:
    """阶段 4 实测：上传不是视频的文件 → status=failed，error.code=InvalidParameter。"""
    async def handler(request):
        return httpx2.Response(200, json={"id": "f", "status": "failed", "error": error_body})

    with pytest.raises(kind, match=message):
        await client(handler).file_status(KEY, "f")


async def test_audio_too_short_fails_with_reason() -> None:
    """内嵌音频切段后最后一段短于 50ms：400 InvalidParameter，param=“audio_url duration”（技术选型 11.6）。"""
    async def handler(request):
        return error(400, "InvalidParameter", "audio_url duration")

    with pytest.raises(TaskFailed, match=r"^音频过短或无法解析"):
        await client(handler).analyze(KEY, "f", "p", 1)
