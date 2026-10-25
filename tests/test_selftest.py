"""设置页的“测试”（技术选型 13.9）：每种结果的说明、同一时间只能跑一个、110 秒时限。"""

import asyncio
import json
from pathlib import Path

import pytest

from app.db import repo
from app.domain.errors import KEY_INVALID, MODEL_UNAVAILABLE, Conflict, GlobalError, NetworkError, RateLimited
from app.services.cloud import Cloud
from app.services.selftest import ASSET, SelfTest
from fakes import RESULT, FakeArk, FakeClock
from world import World

pytestmark = pytest.mark.anyio


@pytest.fixture
def env(tmp_path: Path):
    clock = FakeClock()
    w = World(tmp_path, clock)
    ark = FakeArk(clock)
    w.conn.execute("UPDATE settings SET api_key = 'ark-key-1111' WHERE id = 1")
    return w, ark, SelfTest(w.conn, ark, Cloud(w.conn, ark, clock), clock)


def transcript(*paragraphs: str) -> str:
    return json.dumps({**RESULT, "transcript": list(paragraphs)}, ensure_ascii=False)


def test_asset_is_committed() -> None:
    assert ASSET.is_file() and 0 < ASSET.stat().st_size < 200_000


async def test_success_and_cleanup(env) -> None:
    w, ark, selftest = env
    ark.results = [transcript("This is the vidlex self test.", "The secret word is GIRAFFE.")]
    assert await selftest.run() == (True, "测试成功")
    assert ark.uploads == [ASSET] and ark.files == {} and repo.list_cloud_files(w.conn) == []
    assert repo.get_runtime_state(w.conn)["queue_state"] == "running"  # 不影响队列


async def test_no_key(env) -> None:
    w, ark, selftest = env
    w.conn.execute("UPDATE settings SET api_key = NULL WHERE id = 1")
    assert await selftest.run() == (False, "请先保存 API Key")
    assert ark.uploads == []


@pytest.mark.parametrize("error, message", [
    (GlobalError(KEY_INVALID), "API Key 无效"),
    (GlobalError(MODEL_UNAVAILABLE), "Seed 2.0 Lite 未开通"),
    (NetworkError("连不上"), "无法连接方舟"),
    (RateLimited("限流", 30), "方舟限流，请稍后再试"),
    (GlobalError("云端存储额度已满"), "测试失败：云端存储额度已满"),
])
async def test_error_messages(env, error, message: str) -> None:
    _, ark, selftest = env
    ark.fail("analyze", error)
    assert await selftest.run() == (False, message)
    assert ark.files == {}  # 出错也删除云端副本


async def test_not_heard_and_bad_format(env) -> None:
    _, ark, selftest = env
    ark.results = [transcript("无语音")]
    assert await selftest.run() == (False, "模型没有听出声音")
    ark.results = ["不是 JSON"]
    assert await selftest.run() == (False, "返回格式不合格")


async def test_timeout(env) -> None:
    _, ark, selftest = env
    ark.analyze_seconds = [200]
    assert await selftest.run() == (False, "测试超时（110 秒），请稍后再试")
    assert ark.files == {}


async def test_only_one_at_a_time(env) -> None:
    _, ark, selftest = env
    started = asyncio.Event()

    async def block():
        started.set()
        await asyncio.sleep(0.05)

    ark.on_analyze = block
    first = asyncio.create_task(selftest.run())
    await asyncio.wait_for(started.wait(), 10)  # 出问题时报错，不卡死
    with pytest.raises(Conflict, match="测试正在进行"):
        await selftest.run()
    await first
