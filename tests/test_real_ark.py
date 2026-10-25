"""用真实方舟接口跑通（技术选型 13.14 阶段 4）。默认不运行，会产生少量费用：

    uv run --env-file .env pytest -m real -s
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.db import repo
from app.domain import audio
from app.domain.errors import Retryable
from app.services import media
from app.services.ark_client import ArkClient
from app.services.clock import Clock
from app.services.cloud import Cloud
from app.services.selftest import SelfTest
from app.services.worker import Worker
from world import World

pytestmark = [pytest.mark.real, pytest.mark.anyio]
KEY = os.environ.get("VIDLEX_TEST_ARK_KEY", "")
ORIG = "教程/测试作者&抖音&长颈鹿测试.mp4"


@pytest.fixture
async def env(tmp_path: Path):
    if not KEY:
        pytest.skip("没有设置 VIDLEX_TEST_ARK_KEY")
    clock = Clock()
    w = World(tmp_path, clock)
    repo.save_api_key(w.conn, KEY, clock.now_ms())
    ark = ArkClient.create()
    try:
        yield w, ark, Cloud(w.conn, ark, clock), clock
    finally:
        await ark.aclose()


async def test_selftest(env) -> None:
    w, ark, cloud, clock = env
    assert await SelfTest(w.conn, ark, cloud, clock).run() == (True, "测试成功")
    assert repo.list_cloud_files(w.conn) == []


async def test_end_to_end(env) -> None:
    w, ark, cloud, clock = env
    await w.start()
    src = w.file(f"input/{ORIG}")
    src.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "flite=text='Today we learn about the giraffe. The giraffe is the tallest animal.':voice=slt",
                    "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    source_hash = hashlib.sha256(src.read_bytes()).hexdigest()
    task_id = w.enqueue(ORIG)
    uploaded: list[str] = []
    real_upload = ark.upload

    async def recording_upload(*args):
        file_id = await real_upload(*args)
        uploaded.append(file_id)
        return file_id

    ark.upload = recording_upload  # type: ignore[method-assign]
    worker = Worker(w.conn, w.lib, w.journal, cloud, ark, clock, w.db_path.parent)
    assert await worker.process(task_id) is None
    task = w.task(task_id)
    logs = "\n".join(r["message"] for r in repo.list_logs_before(w.conn, task_id, None, 50))
    assert task["status"] == "completed", (task["fail_reason"], logs)
    md = w.file(task["md_path"]).read_text()
    print(f"\n新标题：{task['title']}\nmd 路径：{task['md_path']}\n--- md ---\n{md}")
    assert "giraffe" in md.lower() or "长颈鹿" in md
    assert 'platform: "抖音"' in md and 'author: "测试作者"' in md
    assert task["current_video_path"].startswith("output/video/")
    assert hashlib.sha256(w.file(task["current_video_path"]).read_bytes()).hexdigest() == source_hash
    # 云端副本和临时文件都已删除
    assert repo.list_cloud_files(w.conn) == [] and w.tmp_entries() == []
    assert uploaded
    for file_id in uploaded:
        with pytest.raises(Retryable, match="ResourceNotFound"):
            await ark.file_status(KEY, file_id)
    shutil.rmtree(w.lib_dir)


SPEECH = "flite=text='Today we learn about the giraffe. The giraffe is the tallest animal.':voice=slt"


async def run_one(env, make_video, rel: str = ORIG):
    w, ark, cloud, clock = env
    await w.start()
    src = w.file(f"input/{rel}")
    src.parent.mkdir(parents=True)
    make_video(src)
    task_id = w.enqueue(rel)
    worker = Worker(w.conn, w.lib, w.journal, cloud, ark, clock, w.db_path.parent)
    assert await worker.process(task_id) is None
    task = w.task(task_id)
    logs = "\n".join(r["message"] for r in repo.list_logs_before(w.conn, task_id, None, 50))
    assert task["status"] == "completed", (task["fail_reason"], logs)
    assert "giraffe" in w.file(task["md_path"]).read_text().lower()
    assert repo.list_cloud_files(w.conn) == [] and w.tmp_entries() == []
    return task, logs


async def test_audio_tail_remainder(env, monkeypatch) -> None:
    """音频正好 30×0.96 秒再多 10ms：2.1 Lite 分析 Files API 文件时固定报“音频过短”，2.0 Lite 能完成（技术选型 11.7）。

    用无损 ALAC 保住精确时长，并临时让它原样上传（正式规则会把 ALAC 转成 AAC-LC）。
    """
    monkeypatch.setattr(audio, "UPLOAD_AS_IS", audio.UPLOAD_AS_IS | {("alac", None)})
    samples = round(28.810 * 16000)

    def make(src: Path) -> None:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", SPEECH,
                        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=29",
                        "-map", "1:v", "-map", "0:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-af", f"aresample=16000,apad,atrim=end_sample={samples}", "-ac", "1", "-c:a", "alac",
                        str(src)], check=True)

    await run_one(env, make)


async def test_oversized_audio_only(env, monkeypatch) -> None:
    """超过 512MB（测试里把上限调小）：只传音频完成。"""
    monkeypatch.setattr(media, "MAX_UPLOAD_BYTES", 1000)

    def make(src: Path) -> None:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", SPEECH,
                        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10", "-shortest",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)

    task, logs = await run_one(env, make)
    assert task["analysis_mode"] == "audio" and "只提取音频" in logs
