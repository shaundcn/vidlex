"""13.11 的接口：每个都测成功、404、409、422；13.12 和第 9 节的负面测试；11 类响应样例。"""

import asyncio
import json
import logging
import shutil
import sqlite3
from urllib.parse import quote

import pytest

from app.api.settings import mask_key
from app.config import Config
from app.db import repo
from app.domain.errors import GlobalError
from app.logging_setup import SecretFilter
from app.services import tasklog
from apphelpers import running
from fakes import FakeArk, FakeClock

pytestmark = pytest.mark.anyio

ORIG = "教程/设计师小王&抖音&PS抠头发教程.mp4"
TITLE = "3 步用 PS 快速抠出发丝"
KEY = "ark-abcdefghijklmnop-5678"


@pytest.fixture
async def api(config: Config, samples):
    ark = FakeArk(FakeClock())
    async with running(config, ark=ark) as (app, client):
        app.state.samples = samples
        app.state.ark = ark
        yield app, client


def db(app) -> sqlite3.Connection:
    return app.state.db


def add_video(app, rel: str = ORIG) -> int:
    lib = app.state.runtime.lib
    dst = lib.path(f"input/{rel}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(app.state.samples["lc.mp4"], dst)
    return repo.enqueue_new_task(db(app), rel, f"input/{rel}", lib.ident(f"input/{rel}"), 1)


async def completed(app) -> int:
    repo.save_api_key(db(app), KEY, 1)
    task_id = add_video(app)
    await app.state.runtime.worker.process(task_id)
    assert repo.get_task(db(app), task_id)["status"] == "completed"
    return task_id


def tables(app) -> dict[str, list[tuple]]:
    return {t: [tuple(r) for r in db(app).execute(f"SELECT * FROM {t} ORDER BY 1")]
            for t in ("tasks", "task_ops", "runtime_state", "settings")}


def no_sql_during(app):
    """记录请求期间执行的 SQL（验证“只读内存”）。"""
    statements: list[str] = []
    db(app).set_trace_callback(statements.append)
    return statements


# ---- 摘要 ----

async def test_summary_and_list_version(api) -> None:
    app, client = api
    first = (await client.get("/api/summary")).json()
    assert first["list_version"] > 1_700_000_000_000  # 起始值是启动时间，重启后不会和旧值重复
    assert first["queue_state"] == "running" and first["counts"] == {
        "queued": 0, "completed": 0, "failed": 0, "stopped": 0, "ignored": 0}
    add_video(app)
    second = (await client.get("/api/summary")).json()
    assert second["counts"]["queued"] == 1 and second["list_version"] > first["list_version"]
    repo.add_log(db(app), 1, "a", 1, "info", "写日志不改版本号")
    repo.set_queue_state(db(app), "paused", None, False, False, 1)
    third = (await client.get("/api/summary")).json()
    assert third["list_version"] == second["list_version"] and third["queue_state"] == "paused"


async def test_summary_reads_memory_only(api) -> None:
    app, client = api
    add_video(app)
    statements = no_sql_during(app)
    for _ in range(3):
        assert (await client.get("/api/summary")).status_code == 200
    assert (await client.get("/api/logs", params={"after_id": app.state.memview.max_log_id})).json() == {
        "items": [], "has_more": False}
    db(app).set_trace_callback(None)
    assert statements == []  # 休眠时页面开着也不会唤醒硬盘


# ---- 任务列表与操作 ----

async def test_empty_list(api) -> None:
    _, client = api
    resp = await client.get("/api/tasks")
    assert resp.status_code == 200 and resp.json() == []


async def test_task_list_fields(api) -> None:
    app, client = api
    task_id = add_video(app)
    [task] = (await client.get("/api/tasks")).json()
    assert task == {"id": task_id, "original_name": "设计师小王&抖音&PS抠头发教程.mp4", "title": None,
                    "created_at": 1, "updated_at": 1, "status": "queued", "step": None, "fail_reason": None,
                    "notice": None, "analysis_mode": None, "allowed_actions": ["stop", "ignore"]}


async def test_start_failed_requeues_and_wakes(api) -> None:
    app, client = api
    task_id = add_video(app)
    db(app).execute("UPDATE tasks SET status='failed', queue_seq=NULL, fail_reason='x', attempts=4 WHERE id=?",
                    (task_id,))
    repo.set_queue_state(db(app), "sleeping", None, False, False, 1)
    resp = await client.post(f"/api/tasks/{task_id}/start", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert (body["status"], body["fail_reason"]) == ("queued", None)
    assert repo.get_task(db(app), task_id)["attempts"] == 0
    assert (await client.get("/api/summary")).json()["queue_state"] == "running"  # 在任务上点开始也唤醒
    assert repo.list_logs_before(db(app), task_id, None, 1)[0]["message"] == "重新排队"


async def test_stop_and_ignore_queued(api) -> None:
    app, client = api
    a, b = add_video(app, "a.mp4"), add_video(app, "b.mp4")
    assert (await client.post(f"/api/tasks/{a}/stop", json={})).json()["status"] == "stopped"
    assert repo.list_logs_before(db(app), a, None, 1)[0]["message"] == "已停止"
    ignored = (await client.post(f"/api/tasks/{b}/ignore", json={})).json()
    assert ignored["status"] == "ignored" and ignored["allowed_actions"] == ["start"]
    assert (await client.post(f"/api/tasks/{b}/start", json={})).json()["status"] == "queued"


async def test_start_completed_redoes(api) -> None:
    app, client = api
    task_id = await completed(app)
    body = (await client.post(f"/api/tasks/{task_id}/start", json={})).json()
    assert body["status"] == "queued" and body["title"] is None


async def test_start_completed_without_video_409(api) -> None:
    app, client = api
    task_id = await completed(app)
    app.state.runtime.lib.path(f"output/video/{TITLE}.mp4").unlink()
    resp = await client.post(f"/api/tasks/{task_id}/start", json={})
    assert resp.status_code == 409 and resp.json() == {"detail": "文件不存在"}
    assert repo.get_task(db(app), task_id)["notice"] == "文件不存在"


async def test_stop_processing(api) -> None:
    app, client = api
    repo.save_api_key(db(app), KEY, 1)
    task_id = add_video(app)
    started = asyncio.Event()

    async def block():
        started.set()
        await asyncio.Event().wait()

    app.state.ark.on_analyze = block
    tick = asyncio.create_task(app.state.runtime.executor_tick())
    await asyncio.wait_for(started.wait(), 10)  # 出问题时报错，不卡死
    [listed] = (await client.get("/api/tasks")).json()
    assert listed["status"] == "processing" and listed["step"] == "analyze" and listed["allowed_actions"] == ["stop"]
    resp = await client.post(f"/api/tasks/{task_id}/stop", json={})
    await tick
    assert resp.status_code == 200 and resp.json()["status"] == "stopped"


async def test_disallowed_action_409_db_unchanged(api) -> None:
    app, client = api
    task_id = add_video(app)  # 排队中不能“开始”
    before = tables(app)
    resp = await client.post(f"/api/tasks/{task_id}/start", json={})
    assert resp.status_code == 409 and resp.json() == {"detail": "当前状态不能执行这个操作"}
    assert tables(app) == before


async def test_commit_step_stop_409(api) -> None:
    app, client = api
    task_id = add_video(app)
    db(app).execute("UPDATE tasks SET status='processing', step='commit', queue_seq=NULL WHERE id=?", (task_id,))
    resp = await client.post(f"/api/tasks/{task_id}/stop", json={})
    assert resp.status_code == 409 and resp.json() == {"detail": "正在保存结果，不能停止"}


async def test_task_404_and_422(api) -> None:
    _, client = api
    assert (await client.post("/api/tasks/999/start", json={})).json() == {"detail": "任务不存在"}
    assert (await client.post("/api/tasks/999/start", json={})).status_code == 404
    assert (await client.post("/api/tasks/abc/start", json={})).status_code == 422
    assert (await client.post("/api/tasks/1/delete", json={})).status_code == 422  # 第一版不做删除


# ---- 查看页、视频 ----

async def test_view_completed(api) -> None:
    app, client = api
    task_id = await completed(app)
    view = (await client.get(f"/api/tasks/{task_id}/view")).json()
    assert view["status"] == "completed" and view["video_exists"] and view["md_exists"]
    assert [i["key"] for i in view["front_matter"]] == ["title", "platform", "author", "tags", "original_file"]
    assert view["front_matter"][0] == {"key": "title", "value": TITLE}
    assert view["front_matter"][3] == {"key": "tags", "value": ""}
    assert "![[" not in view["md_html"] and "<h2>主要内容</h2>" in view["md_html"]


async def test_view_escapes_html(api) -> None:
    app, client = api
    task_id = await completed(app)
    md = app.state.runtime.lib.path(f"output/{TITLE}.md")
    md.write_text(md.read_text() + "\n<img src=x onerror=alert(1)>\n\n[点我](javascript:alert(1))\n")
    html = (await client.get(f"/api/tasks/{task_id}/view")).json()["md_html"]
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html
    assert 'href="javascript' not in html


async def test_view_missing_files(api) -> None:
    app, client = api
    task_id = await completed(app)
    lib = app.state.runtime.lib
    lib.path(f"output/{TITLE}.md").unlink()
    view = (await client.get(f"/api/tasks/{task_id}/view")).json()
    assert view["md_exists"] is False and view["md_html"] is None and view["video_exists"]
    lib.path(f"output/video/{TITLE}.mp4").unlink()
    assert (await client.get(f"/api/tasks/{task_id}/view")).json()["video_exists"] is False
    resp = await client.get(f"/api/tasks/{task_id}/video")
    assert resp.status_code == 404 and resp.json() == {"detail": "文件不存在"}


async def test_view_queued_has_no_md(api) -> None:
    app, client = api
    task_id = add_video(app)
    view = (await client.get(f"/api/tasks/{task_id}/view")).json()
    assert view["md_exists"] is False and view["video_exists"] and view["front_matter"] == []


async def test_video_range(api) -> None:
    app, client = api
    task_id = add_video(app)
    full = await client.get(f"/api/tasks/{task_id}/video")
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
    assert full.headers["accept-ranges"] == "bytes"
    part = await client.get(f"/api/tasks/{task_id}/video", headers={"range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.headers["content-range"].startswith("bytes 0-99/")


async def test_video_404_and_422(api) -> None:
    _, client = api
    assert (await client.get("/api/tasks/999/video")).status_code == 404
    assert (await client.get("/api/tasks/abc/video")).status_code == 422
    assert (await client.get("/api/tasks/999/view")).json() == {"detail": "任务不存在"}


async def test_md_download(api) -> None:
    app, client = api
    task_id = await completed(app)
    resp = await client.get(f"/api/tasks/{task_id}/md")
    assert resp.status_code == 200 and resp.headers["content-type"] == "text/markdown; charset=utf-8"
    # 中文文件名按 RFC 5987 编码，浏览器下载时显示原名
    assert resp.headers["content-disposition"] == f"attachment; filename*=utf-8''{quote(TITLE + '.md')}"
    assert resp.content == app.state.runtime.lib.path(f"output/{TITLE}.md").read_bytes()  # 原样，含信息块和嵌入行


async def test_md_download_404_and_422(api) -> None:
    app, client = api
    queued = add_video(app, "排队中.mp4")  # 还没有 md
    assert (await client.get(f"/api/tasks/{queued}/md")).json() == {"detail": "文件不存在"}
    task_id = await completed(app)
    app.state.runtime.lib.path(f"output/{TITLE}.md").unlink()
    resp = await client.get(f"/api/tasks/{task_id}/md")
    assert resp.status_code == 404 and resp.json() == {"detail": "文件不存在"}
    assert (await client.get("/api/tasks/999/md")).json() == {"detail": "任务不存在"}
    assert (await client.get("/api/tasks/abc/md")).status_code == 422


# ---- 设置 ----

async def test_settings_defaults(api) -> None:
    _, client = api
    s = (await client.get("/api/settings")).json()
    assert s["has_api_key"] is False and s["api_key_masked"] is None
    assert s["prompts"] == s["default_prompts"]
    assert s["sleep"] == {"enabled": True, "idle_minutes": 30, "interval_hours": 2}
    assert s["folders"] == [{"name": n, "ok": True, "reason": None} for n in ("input", "output", "ignored", "data")]


def test_mask_key() -> None:
    assert mask_key("abcdefghij") == "abcd**ghij"
    assert mask_key("abcdefgh") == "********"


async def test_api_key_never_returned_or_logged(api, caplog: pytest.LogCaptureFixture) -> None:
    app, client = api
    resp = await client.put("/api/settings/api-key", json={"key": f"  {KEY}  "})
    masked = "ark-" + "*" * (len(KEY) - 8) + "5678"
    assert resp.json() == {"saved": True, "masked": masked}
    assert repo.get_settings(db(app))["api_key"] == KEY  # 去掉首尾空格保存
    settings = await client.get("/api/settings")
    assert settings.json()["api_key_masked"] == masked and KEY not in settings.text
    with caplog.at_level(logging.INFO):
        logging.getLogger("vidlex.test").info("请求头 Authorization: Bearer %s", KEY)
    for record in caplog.records:
        SecretFilter().filter(record)
        assert KEY not in record.getMessage()
    tasklog.write(db(app), 1, 1, "a", "info", f"错误信息里带了 {KEY}")
    assert KEY not in repo.list_logs_before(db(app), 1, None, 1)[0]["message"]


@pytest.mark.parametrize("key", ["", "   ", "x" * 201, "ark-****************5678"])
async def test_api_key_422(api, key: str) -> None:
    _, client = api
    assert (await client.put("/api/settings/api-key", json={"key": key})).status_code == 422


async def test_prompts(api) -> None:
    app, client = api
    prompts = {"title": " 新标题 ", "main_content": "主要", "highlight": "亮点", "transcript": "转写"}
    resp = await client.put("/api/settings/prompts", json=prompts)
    assert resp.status_code == 200 and resp.json()["title"] == "新标题"
    assert repo.get_settings(db(app))["prompt_title"] == "新标题"
    for bad in ({**prompts, "title": "  "}, {**prompts, "highlight": "字" * 2001}, {"title": "缺字段"}):
        before = tables(app)
        resp = await client.put("/api/settings/prompts", json=bad)
        assert resp.status_code == 422 and tables(app) == before


@pytest.mark.parametrize("body", [{"enabled": True, "idle_minutes": 4, "interval_hours": 2},
                                  {"enabled": True, "idle_minutes": 30, "interval_hours": 25},
                                  {"enabled": True, "idle_minutes": 30.5, "interval_hours": 2}])
async def test_sleep_422(api, body) -> None:
    _, client = api
    resp = await client.put("/api/settings/sleep", json=body)
    assert resp.status_code == 422 and resp.json()["detail"][0]["loc"][0] == "body"


async def test_sleep_save_and_disable_wakes(api) -> None:
    app, client = api
    repo.set_queue_state(db(app), "sleeping", None, False, False, 1)
    body = {"enabled": False, "idle_minutes": 60, "interval_hours": 3}
    resp = await client.put("/api/settings/sleep", json=body)
    assert resp.status_code == 200 and resp.json() == body
    assert (await client.get("/api/settings")).json()["sleep"] == body
    assert (await client.get("/api/summary")).json()["queue_state"] == "running"


async def test_folder_status_on_storage_fault(api) -> None:
    app, client = api
    app.state.runtime.enter_error(GlobalError("存储故障：找不到视频库文件夹，请检查挂载", storage=True))
    folders = (await client.get("/api/settings")).json()["folders"]
    assert folders[2] == {"name": "ignored", "ok": False, "reason": "存储故障：找不到视频库文件夹，请检查挂载"}


# ---- 队列 ----

async def test_queue_actions(api) -> None:
    app, client = api
    resp = await client.post("/api/queue/pause", json={})
    assert resp.status_code == 200 and resp.json()["queue_state"] == "paused"
    again = await client.post("/api/queue/pause", json={})
    assert again.status_code == 409 and again.json() == {"detail": "队列当前状态不能执行这个操作"}
    assert (await client.post("/api/queue/resume", json={})).json()["queue_state"] == "running"
    repo.set_queue_state(db(app), "sleeping", None, False, False, 1)
    assert (await client.post("/api/queue/wake", json={})).json()["queue_state"] == "running"
    assert (await client.post("/api/queue/explode", json={})).status_code == 422


async def test_resume_from_storage_fault(api) -> None:
    app, client = api
    lib = app.state.runtime.lib
    lib.path(".vidlex/library-id").write_text("other\n")
    app.state.runtime.enter_error(GlobalError("存储故障", storage=True))
    resp = await client.post("/api/queue/resume", json={})
    assert resp.status_code == 409 and "标识与记录不一致" in resp.json()["detail"]
    lib.path(".vidlex/library-id").write_text(repo.get_runtime_state(db(app))["library_id"] + "\n")
    assert (await client.post("/api/queue/resume", json={})).json()["queue_state"] == "running"


# ---- 日志 ----

async def test_logs_paging(api) -> None:
    app, client = api
    ids = [repo.add_log(db(app), 1 if i % 2 else 2, f"任务{i % 2}", 1, "info", f"第 {i} 条") for i in range(5)]
    page = (await client.get("/api/logs", params={"limit": 2})).json()
    assert [i["id"] for i in page["items"]] == [ids[4], ids[3]] and page["has_more"] is True
    older = (await client.get("/api/logs", params={"limit": 2, "before_id": ids[3]})).json()
    assert [i["id"] for i in older["items"]] == [ids[2], ids[1]] and older["has_more"] is True
    newer = (await client.get("/api/logs", params={"after_id": ids[1], "limit": 2})).json()
    assert [i["id"] for i in newer["items"]] == [ids[2], ids[3]] and newer["has_more"] is True
    only1 = (await client.get("/api/logs", params={"task_id": 1})).json()
    assert {i["task_id"] for i in only1["items"]} == {1} and only1["has_more"] is False
    assert (await client.get("/api/logs", params={"after_id": 1, "before_id": 3})).status_code == 422
    assert (await client.get("/api/logs", params={"limit": 101})).status_code == 422


async def test_log_tasks(api) -> None:
    app, client = api
    now = app.state.runtime.clock.now_ms()
    repo.add_log(db(app), 1, "旧名字", now, "info", "a")
    repo.add_log(db(app), 1, "新名字", now, "info", "b")
    repo.add_log(db(app), 2, "很久以前", now - 31 * 86400_000, "info", "c")
    assert (await client.get("/api/logs/tasks")).json() == [{"task_id": 1, "task_name": "新名字"}]


async def test_selftest_endpoint(api) -> None:
    app, client = api
    resp = await client.post("/api/settings/test", json={})
    assert resp.status_code == 200 and resp.json() == {"ok": False, "message": "请先保存 API Key"}
    repo.save_api_key(db(app), KEY, 1)
    app.state.ark.results = [json.dumps({
        "title": "t", "main_content": {"overview": "o", "sections": [], "conclusion": ""},
        "highlight": "", "transcript": ["the secret word is giraffe"],
    })]
    resp = await client.post("/api/settings/test", json={})
    assert resp.json() == {"ok": True, "message": "测试成功"}


async def test_selftest_409_while_running(api) -> None:
    app, client = api
    repo.save_api_key(db(app), KEY, 1)
    started = asyncio.Event()

    async def block():
        started.set()
        await asyncio.sleep(0.05)

    app.state.ark.on_analyze = block
    first = asyncio.create_task(client.post("/api/settings/test", json={}))
    await asyncio.wait_for(started.wait(), 10)  # 出问题时报错，不卡死
    second = await client.post("/api/settings/test", json={})
    assert second.status_code == 409 and second.json() == {"detail": "测试正在进行，请稍候"}
    await first


# ---- 前端静态文件（技术选型 12.2、12.6、13.12）----

async def test_static_pages(api) -> None:
    _, client = api
    index = await client.get("/")
    assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
    assert '<html lang="zh-CN">' in index.text and 'src="js/pages/tasks.js"' in index.text
    assert index.headers["cache-control"] == "no-cache"
    for path, kind in (("/settings.html", "text/html"), ("/view.html", "text/html"), ("/base.css", "text/css"),
                       ("/js/core/api.js", "javascript"), ("/fonts/open-sans.woff2", "font/woff2")):
        resp = await client.get(path)
        assert resp.status_code == 200 and kind in resp.headers["content-type"], path
        assert resp.headers["cache-control"] == "no-cache"
    assert (await client.get("/no-such-page.html")).status_code == 404
    api_resp = await client.get("/api/summary")
    assert "cache-control" not in api_resp.headers  # 接口不加


async def test_static_does_not_shadow_api(api) -> None:
    _, client = api
    assert (await client.get("/api/tasks")).headers["content-type"] == "application/json"
    assert (await client.get("/health")).json()["status"] == "ok"
