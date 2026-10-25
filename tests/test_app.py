"""启动检查、/health、访问不受限制、统一错误处理、日志过滤（技术选型 4.9、13.12、13.13）。"""

import hashlib
import logging
import os
from pathlib import Path

import pytest

from app import logging_setup
from app.config import Config
from app.db import connection, repo
from app.main import create_app
from apphelpers import running

pytestmark = pytest.mark.anyio


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
        for p in sorted(root.rglob("*"))
    }


async def test_health_ok_and_defaults_written(config: Config) -> None:
    async with running(config) as (app, client):
        resp = await client.get("/health")
        settings = repo.get_settings(app.state.db)
        state = repo.get_runtime_state(app.state.db)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["schema_version"] == 2
    assert body["ffmpeg"] is None or body["ffmpeg"].startswith("ffmpeg version")
    assert settings["prompt_title"] == "视频的主题。20 字以内。"
    assert state["queue_state"] == "running"


async def test_data_survives_restart(config: Config) -> None:
    async with running(config):
        pass
    video = config.library_dir / "input" / "a.mp4"
    video.write_bytes(b"video")
    st = video.stat()
    conn = connection.open_database(config.db_path)
    task_id = repo.enqueue_new_task(conn, "a.mp4", "input/a.mp4", (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns), 5)
    conn.execute("UPDATE settings SET sleep_idle_minutes = 45 WHERE id = 1")
    conn.close()
    async with running(config, start_background=True) as (app, client):
        assert (await client.get("/health")).status_code == 200
        assert [r["id"] for r in repo.list_tasks(app.state.db)] == [task_id]
        assert repo.get_settings(app.state.db)["sleep_idle_minutes"] == 45


async def test_corrupt_database_degrades_without_touching_files(config: Config) -> None:
    (config.library_dir / "input" / "教程").mkdir(parents=True)
    (config.library_dir / "input" / "教程" / "a.mp4").write_bytes(b"video")
    config.data_dir.mkdir()
    config.db_path.write_bytes(os.urandom(8192))
    library_before = snapshot(config.library_dir)
    data_before = snapshot(config.data_dir)
    async with running(config, start_background=True) as (app, client):
        resp = await client.get("/health")
        summary = await client.get("/api/summary")
        tasks = await client.get("/api/tasks")
        assert app.state.db is None
    assert resp.status_code == 503 and resp.json() == {"detail": "数据库文件损坏"}
    # 降级模式：摘要仍返回 200 并显示原因，其他接口 503（13.11）
    assert summary.status_code == 200
    assert summary.json()["queue_state"] == "error" and summary.json()["error_message"] == "数据库文件损坏"
    assert tasks.status_code == 503 and tasks.json() == {"detail": "数据库文件损坏"}
    assert snapshot(config.library_dir) == library_before
    assert snapshot(config.data_dir) == data_before


async def test_newer_database_degrades(config: Config) -> None:
    conn = connection.open_database(config.db_path)
    conn.execute("PRAGMA user_version=3")
    conn.close()
    async with running(config) as (_, client):
        resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "数据库版本比程序新，请升级 vidlex"}


# ---- 访问不受限制 ----

@pytest.fixture
async def client(config: Config):
    async with running(config) as (_, c):
        yield c


async def test_no_access_restrictions(client) -> None:
    """不限制访问地址、来源和请求格式（2026-10-25 用户决定）。"""
    assert (await client.get("/health", headers={"host": "video.example.com"})).status_code == 200
    resp = await client.post("/health", data={"a": "1"}, headers={"origin": "http://other.example.com"})
    assert resp.status_code == 405  # 中间件放行，路由只有 GET


async def test_unexpected_error_500(config: Config, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(config, start_background=False)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("测试用的意外错误")

    app.router.routes.insert(0, app.router.routes.pop())  # 排在静态文件挂载“/”之前

    async with running(config, app=app) as (_, c):
        resp = await c.get("/boom")
    assert resp.status_code == 500 and resp.json() == {"detail": "服务器内部错误"}
    assert "测试用的意外错误" in caplog.text and "Traceback" in caplog.text  # 完整堆栈进了日志


# ---- 日志过滤 ----

def make_record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("x", logging.INFO, __file__, 1, msg, args, None)


def test_secret_filter_masks_key_and_bearer() -> None:
    logging_setup.set_api_key("ark-1234567890abcd")
    try:
        record = make_record("key=%s header=%s", "ark-1234567890abcd", "Bearer sk-zzzz9999")
        logging_setup.SecretFilter().filter(record)
        text = record.getMessage()
    finally:
        logging_setup.set_api_key(None)
    assert "ark-1234567890abcd" not in text and "***abcd" in text
    assert "sk-zzzz9999" not in text and "Bearer ***9999" in text


@pytest.mark.parametrize(
    "path, status, kept",
    [("/health", 200, False), ("/api/summary", 200, False), ("/api/logs?after_id=5", 200, False),
     ("/api/logs?before_id=5", 200, True), ("/health", 503, True),
     ("/api/tasks/58/ignore", 200, True)],
)
def test_quiet_poll_filter(path: str, status: int, kept: bool) -> None:
    record = make_record('%s - "%s %s HTTP/%s" %d', "192.168.1.20:5000", "GET", path, "1.1", status)
    assert logging_setup.QuietPollFilter().filter(record) is kept


def test_quiet_poll_filter_keeps_other_records() -> None:
    """不是访问日志格式的记录（args 不是 5 项）照常输出。"""
    assert logging_setup.QuietPollFilter().filter(make_record("普通日志 %s", "x")) is True


def test_setup_logging_masks_key_in_traceback(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """日志统一从 stdout 输出；异常堆栈里出现的 Key 同样被替换（技术选型 13.13）。"""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", list(root.handlers))  # 测试结束后恢复原来的 handler
    monkeypatch.setattr(root, "level", root.level)
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "filters", list(access.filters))
    logging_setup.setup_logging()
    logging_setup.set_api_key("ark-1234567890abcd")
    try:
        try:
            raise RuntimeError("请求头 Authorization: Bearer ark-1234567890abcd")
        except RuntimeError:
            logging.getLogger("vidlex.test").exception("调用失败 key=%s", "ark-1234567890abcd")
    finally:
        logging_setup.set_api_key(None)
    out = capsys.readouterr().out
    assert "ark-1234567890abcd" not in out
    assert "调用失败 key=***abcd" in out and "Traceback" in out and "Bearer ***abcd" in out
    assert len(root.handlers) == 1 and any(isinstance(f, logging_setup.QuietPollFilter) for f in access.filters)
