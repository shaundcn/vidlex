"""云端副本的登记、删除、后台清理（立项第 3 节第 7 步，技术选型 13.9）。"""

from pathlib import Path

import pytest

from app.db import repo
from app.domain.errors import NetworkError, Retryable
from app.services.cloud import Cloud
from fakes import FakeArk, FakeClock
from world import World

pytestmark = pytest.mark.anyio

KEY = "ark-test-key-1234"
DAY_MS = 86400 * 1000


@pytest.fixture
async def env(tmp_path: Path):
    clock = FakeClock()
    w = World(tmp_path, clock)
    await w.start()
    ark = FakeArk(clock)
    return w, ark, Cloud(w.conn, ark, clock), clock


def registered(w: World) -> list[str]:
    return [r["file_id"] for r in repo.list_cloud_files(w.conn)]


def leftover(w: World, ark: FakeArk, file_id: str, expire_at_ms: int) -> None:
    """上次没删掉、留在登记表里的副本（不是本次进程上传的，不算正在使用）。"""
    repo.add_cloud_file(w.conn, file_id, expire_at_ms, 1)
    ark.files[file_id] = {}


async def test_cleanup_deletes_leftovers(env) -> None:
    w, ark, cloud, clock = env
    leftover(w, ark, "f1", clock.now_ms() + DAY_MS)
    leftover(w, ark, "f2", clock.now_ms() + DAY_MS)
    await cloud.cleanup(KEY)
    assert registered(w) == [] and sorted(ark.deleted) == ["f1", "f2"]


async def test_expired_dropped_without_calling_ark(env) -> None:
    """过期的副本方舟已经自动删掉了（换了 Key 后删不掉的旧副本也靠这条停止重试）。"""
    w, ark, cloud, clock = env
    leftover(w, ark, "old", clock.now_ms() - 1)
    await cloud.cleanup(None)  # 没有 Key 也能注销过期的
    assert registered(w) == [] and ark.deleted == []


async def test_no_key_keeps_unexpired(env) -> None:
    w, ark, cloud, clock = env
    leftover(w, ark, "f1", clock.now_ms() + DAY_MS)
    await cloud.cleanup(None)
    assert registered(w) == ["f1"] and ark.deleted == []


async def test_network_error_stops_this_round(env) -> None:
    w, ark, cloud, clock = env
    leftover(w, ark, "f1", clock.now_ms() + DAY_MS)
    leftover(w, ark, "f2", clock.now_ms() + DAY_MS)
    ark.fail("delete", NetworkError("连不上"))
    await cloud.cleanup(KEY)
    assert registered(w) == ["f1", "f2"]  # 断网：这一轮都不动，下一轮再试
    await cloud.cleanup(KEY)
    assert registered(w) == []


async def test_other_error_skips_only_that_file(env) -> None:
    w, ark, cloud, clock = env
    leftover(w, ark, "f1", clock.now_ms() + DAY_MS)
    leftover(w, ark, "f2", clock.now_ms() + DAY_MS)
    ark.fail("delete", Retryable("方舟服务端错误 500"))
    await cloud.cleanup(KEY)
    assert registered(w) == ["f1"] and ark.deleted == ["f2"]


async def test_in_use_skipped_until_released(env) -> None:
    """正在处理的任务刚上传的副本不能被后台清理删掉（2026-10-24 NAS 上任务 32 的 404）。"""
    w, ark, cloud, clock = env
    cloud.register("busy", clock.now_ms() // 1000 + 86400)
    ark.files["busy"] = {}
    await cloud.cleanup(KEY)
    assert registered(w) == ["busy"] and ark.deleted == []
    assert await cloud.delete(KEY, "busy") is True
    assert registered(w) == [] and cloud.in_use == set()


async def test_delete_failure_releases_and_keeps_registration(env) -> None:
    w, ark, cloud, clock = env
    cloud.register("f1", clock.now_ms() // 1000 + 86400)
    ark.files["f1"] = {}
    ark.fail("delete", Retryable("方舟服务端错误 502"))
    assert await cloud.delete(KEY, "f1") is False
    assert registered(w) == ["f1"] and cloud.in_use == set()  # 交给后台清理
    await cloud.cleanup(KEY)
    assert registered(w) == []


async def test_delete_without_key(env) -> None:
    w, _, cloud, clock = env
    cloud.register("f1", clock.now_ms() // 1000 + 86400)
    assert await cloud.delete(None, "f1") is False
    assert registered(w) == ["f1"] and cloud.in_use == set()
