"""处理一个任务：按立项 8.1、8.3、8.4、8.5、8.6 逐行测试（使用模拟的方舟和假时钟）。"""

import asyncio
import hashlib
import json
import shutil
from collections import namedtuple
from pathlib import Path

import pytest

from app.db import repo
from app.domain.errors import (
    FILE_BROKEN, KEY_INVALID, MODEL_UNAVAILABLE, NO_API_KEY, NO_SPACE, NOT_VIDEO,
    OVERSIZED_NO_AUDIO, OVERSIZED_TOO_LONG, TRANSCODED_OVERSIZED, GlobalError, NetworkError,
    RateLimited, Retryable, TaskFailed,
)
from app.domain import audio
from app.domain.audio import Stream
from app.domain.prompt import AUDIO_ONLY_HINT
from app.services import media
from app.services.cloud import Cloud
from app.services.scanner import Scanner
from app.services.worker import Worker
from fakes import RESULT, FakeArk, FakeClock
from world import ORIG, TITLE, World

pytestmark = pytest.mark.anyio

VIDEO_IN = f"input/{ORIG}"
MD = f"output/{TITLE}.md"  # 视频在 input/教程/ 里，md 不保留这层目录
DAY = 86400


class Env:
    def __init__(self, tmp_path: Path, sample: Path, rel: str = ORIG) -> None:
        self.clock = FakeClock()
        self.w = World(tmp_path, self.clock)
        self.ark = FakeArk(self.clock)
        self.cloud = Cloud(self.w.conn, self.ark, self.clock)
        self.worker = Worker(self.w.conn, self.w.lib, self.w.journal, self.cloud, self.ark, self.clock,
                             self.w.db_path.parent)
        self.w.conn.execute("UPDATE settings SET api_key = 'ark-test-key-1234' WHERE id = 1")
        self.sample = sample
        self.rel = rel

    async def setup(self) -> int:
        await self.w.start()
        dst = self.w.file(f"input/{self.rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.sample, dst)
        self.source_hash = hashlib.sha256(dst.read_bytes()).hexdigest()
        return self.w.enqueue(self.rel)

    def task(self, task_id: int):
        return self.w.task(task_id)

    def logs(self, task_id: int) -> str:
        return "\n".join(r["message"] for r in repo.list_logs_before(self.w.conn, task_id, None, 100))

    def queue(self):
        return repo.get_runtime_state(self.w.conn)

    def assert_cleaned(self, task_id: int) -> None:
        assert self.ark.files == {}, "云端副本没删"
        assert repo.list_cloud_files(self.w.conn) == []
        assert not self.w.file(f".vidlex/tmp/{task_id}").exists()


@pytest.fixture
def env(tmp_path: Path, samples: dict[str, Path]) -> Env:
    return Env(tmp_path, samples["lc.mp4"])


# ---- 正常流程（立项第 3 节）----

async def test_success(env: Env) -> None:
    task_id = await env.setup()
    start_s = env.clock.now_ms() // 1000
    assert await env.worker.process(task_id) is None
    task = env.task(task_id)
    assert (task["status"], task["title"], task["md_path"]) == ("completed", TITLE, MD)
    assert task["current_video_path"] == f"output/video/{TITLE}.mp4"
    md = env.w.file(MD).read_text()
    assert md.startswith('---\ntitle: "3 步用 PS 快速抠出发丝"\nplatform: "抖音"\nauthor: "设计师小王"\n')
    assert f"![[{TITLE}.mp4]]" in md and "## 转写\n大家好。" in md
    # 上传的是原视频本身，过期时间 = 当前 + 1 天 + 1 小时；回答比视频早 1 小时过期（立项附录 B）
    assert env.ark.uploads == [env.w.file(VIDEO_IN)]
    assert env.ark.upload_expire[0] == start_s + DAY + 3600
    assert env.ark.analyze_expire[0] == env.ark.upload_expire[0] - 3600
    assert env.ark.prompts[0].startswith("title：视频的主题。")
    assert "开始处理。提示词：\ntitle：" in env.logs(task_id)
    assert hashlib.sha256(env.w.file(task["current_video_path"]).read_bytes()).hexdigest() == env.source_hash
    env.assert_cleaned(task_id)
    assert env.queue()["consecutive_failures"] == 0


async def test_numbering_when_names_taken(env: Env) -> None:
    task_id = await env.setup()
    env.w.file(f"output/video/{TITLE}.mp4").write_bytes(b"other")  # 只在 video 里重名，md 也要跟着编号
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["md_path"] == f"output/{TITLE}(1).md"
    assert task["current_video_path"] == f"output/video/{TITLE}(1).mp4"
    assert f"![[{TITLE}(1).mp4]]" in env.w.file(task["md_path"]).read_text()
    assert env.w.file(f"output/video/{TITLE}.mp4").read_bytes() == b"other"


async def test_numbering_when_md_name_taken(env: Env) -> None:
    task_id = await env.setup()
    env.w.file(MD).write_text("别的文件夹里同标题视频的 md")  # md 平铺后，不同文件夹的同名标题会撞名
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["md_path"] == f"output/{TITLE}(1).md"
    assert task["current_video_path"] == f"output/video/{TITLE}(1).mp4"  # 视频跟着编号
    assert env.w.file(MD).read_text() == "别的文件夹里同标题视频的 md"


# ---- 8.1 重试、时限、影响所有任务 ----

async def test_retry_then_success(env: Env) -> None:
    task_id = await env.setup()
    env.ark.fail("analyze", Retryable("方舟服务端错误 503"), Retryable("方舟服务端错误 502"))
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["status"] == "completed" and task["attempts"] == 2
    assert [s for s in env.clock.slept if s >= 60] == [60, 300]
    assert "第 3 次尝试将在 5 分钟后开始" in env.logs(task_id)
    env.assert_cleaned(task_id)  # 每次尝试上传的副本都已删除


async def test_retry_exhausted_fails(env: Env) -> None:
    task_id = await env.setup()
    env.ark.fail("analyze", *[Retryable(f"错误 {i}") for i in range(4)])
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert (task["status"], task["fail_reason"], task["attempts"]) == ("failed", "错误 3", 4)
    assert task["current_video_path"] == VIDEO_IN  # 视频留在 input
    assert [s for s in env.clock.slept if s >= 60] == [60, 300, 900]
    assert len(env.ark.uploads) == 4
    assert env.queue()["consecutive_failures"] == 1
    env.assert_cleaned(task_id)


async def test_interrupted_attempts_count(env: Env) -> None:
    task_id = await env.setup()
    env.w.conn.execute("UPDATE tasks SET attempts = 2 WHERE id = ?", (task_id,))  # 重启中断过 2 次
    env.ark.fail("analyze", Retryable("a"), Retryable("b"))
    await env.worker.process(task_id)
    assert env.task(task_id)["status"] == "failed"  # 共 4 次尝试


async def test_rate_limit_does_not_consume_attempts(env: Env) -> None:
    task_id = await env.setup()
    env.ark.fail("analyze", *[RateLimited("限流", 30) for _ in range(5)])
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["status"] == "completed" and task["attempts"] == 0
    assert env.clock.slept.count(30) == 5


async def test_rate_limit_waits_count_toward_60_minutes(env: Env) -> None:
    task_id = await env.setup()
    env.ark.fail("analyze", *[RateLimited("限流", 1000) for _ in range(4)])
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["status"] == "completed" and task["attempts"] == 1
    assert "单次尝试超过 60 分钟" in env.logs(task_id)


async def test_attempt_timeout_is_one_failure(env: Env) -> None:
    task_id = await env.setup()
    env.ark.analyze_seconds = [3601, 10]
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["status"] == "completed" and task["attempts"] == 1


@pytest.mark.parametrize("message", [KEY_INVALID, MODEL_UNAVAILABLE, "云端存储额度已满"])
async def test_global_error_requeues_front(env: Env, message: str) -> None:
    task_id = await env.setup()
    env.w.conn.execute("UPDATE tasks SET attempts = 1 WHERE id = ?", (task_id,))
    env.ark.fail("analyze", Retryable("先失败一次"), GlobalError(message))
    error = await env.worker.process(task_id)
    assert isinstance(error, GlobalError)
    task = env.task(task_id)
    assert (task["status"], task["attempts"]) == ("queued", 1)  # 本轮尝试不计入
    assert repo.next_queued(env.w.conn)["id"] == task_id  # 恢复后先处理它
    state = env.queue()
    assert (state["queue_state"], state["error_message"], state["consecutive_failures"]) == ("error", message, 0)
    env.assert_cleaned(task_id)


async def test_no_api_key(env: Env) -> None:
    task_id = await env.setup()
    env.w.conn.execute("UPDATE settings SET api_key = NULL WHERE id = 1")
    await env.worker.process(task_id)
    assert env.task(task_id)["status"] == "queued"
    assert env.queue()["error_message"] == NO_API_KEY


async def test_short_network_outage_retries(env: Env) -> None:
    task_id = await env.setup()
    env.ark.fail("upload", NetworkError("连不上"), NetworkError("连不上"))
    await env.worker.process(task_id)
    assert env.task(task_id)["status"] == "completed"
    assert env.clock.slept[:2] == [10, 30]


async def test_long_network_outage_enters_auto_error(env: Env) -> None:
    task_id = await env.setup()
    env.ark.fail("upload", *[NetworkError("连不上") for _ in range(4)])
    await env.worker.process(task_id)
    state = env.queue()
    assert (state["queue_state"], state["error_auto_resume"]) == ("error", 1)
    assert env.task(task_id)["status"] == "queued"


# ---- 8.3 方舟那一侧 ----

@pytest.mark.parametrize("reason", ["内容审核没通过", "内容过长", "音频过短"])
async def test_non_retryable_failures(env: Env, reason: str) -> None:
    task_id = await env.setup()
    env.ark.fail("analyze", TaskFailed(reason))
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert (task["status"], task["fail_reason"], task["attempts"]) == ("failed", reason, 0)
    assert len(env.ark.uploads) == 1
    env.assert_cleaned(task_id)


async def test_preprocess_timeout_retries_then_fails(env: Env) -> None:
    """不再生成 720p 副本：预处理超时按 8.1 重试，仍然超时就失败（立项 8.3）。"""
    task_id = await env.setup()
    env.ark.polls_before_active = [10_000] * 4
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert (task["status"], task["fail_reason"], task["attempts"]) == ("failed", "视频预处理超时", 4)
    assert env.ark.uploads == [env.w.file(VIDEO_IN)] * 4  # 每次都原样上传
    env.assert_cleaned(task_id)


async def test_preprocess_failed_status_retries(env: Env, monkeypatch) -> None:
    task_id = await env.setup()
    states = iter(["failed"])
    original = env.ark.file_status

    async def status(key, file_id):
        return next(states, None) or await original(key, file_id)

    monkeypatch.setattr(env.ark, "file_status", status)
    await env.worker.process(task_id)
    assert env.task(task_id)["status"] == "completed" and env.task(task_id)["attempts"] == 1


@pytest.mark.parametrize("name", ["garbage.mp4", "truncated.mp4"])
async def test_broken_file_fails_without_upload(tmp_path: Path, samples, name: str) -> None:
    env = Env(tmp_path, samples[name])
    task_id = await env.setup()
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert (task["status"], task["fail_reason"]) == ("failed", FILE_BROKEN)
    assert env.ark.uploads == []


async def test_unrecognized_audio_transcoded(tmp_path: Path, samples, monkeypatch) -> None:
    """E-AC3 等方舟认不准的音频先转成 AAC-LC，上传临时副本；归档的仍是原视频（技术选型 11.5）。"""
    env = Env(tmp_path, samples["eac3.mov"], "教程/a&b&c.mov")
    task_id = await env.setup()
    uploaded: list[list] = []
    original = env.ark.upload

    async def upload(key, path, expire_at):
        uploaded.append(await media.check_readable(path))
        return await original(key, path, expire_at)

    monkeypatch.setattr(env.ark, "upload", upload)
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["status"] == "completed" and task["current_video_path"] == f"output/video/{TITLE}.mov"
    assert env.ark.uploads == [env.w.file(f".vidlex/tmp/{task_id}/upload.mov")]
    assert uploaded[0].streams[1] == Stream("audio", "aac", "LC", False)
    assert "音频为 eac3，转成 AAC-LC 后上传" in env.logs(task_id)
    assert hashlib.sha256(env.w.file(task["current_video_path"]).read_bytes()).hexdigest() == env.source_hash
    env.assert_cleaned(task_id)


async def test_not_video_fails_without_upload(tmp_path: Path, samples) -> None:
    """扩展名是 .mp4、实际是 mp3（2026-10-24 NAS 上遇到）：不上传，提示不是视频。"""
    env = Env(tmp_path, samples["audio_only.mp4"])
    task_id = await env.setup()
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert (task["status"], task["fail_reason"]) == ("failed", NOT_VIDEO)
    assert env.ark.uploads == []


async def test_transcoded_oversized_fails(tmp_path: Path, samples, monkeypatch) -> None:
    env = Env(tmp_path, samples["eac3.mov"])
    task_id = await env.setup()
    monkeypatch.setattr(media, "MAX_UPLOAD_BYTES", env.w.file(VIDEO_IN).stat().st_size)
    real = media.transcode_audio

    async def bigger(src, dst):
        await real(src, dst)
        with open(dst, "ab") as f:
            f.write(b"\0" * (src.stat().st_size + 1))

    monkeypatch.setattr(media, "transcode_audio", bigger)
    await env.worker.process(task_id)
    assert env.task(task_id)["fail_reason"] == TRANSCODED_OVERSIZED and env.ark.uploads == []
    env.assert_cleaned(task_id)


# ---- 和后台清理、用户操作同时发生 ----

async def test_cleanup_during_processing_keeps_file_in_use(env: Env) -> None:
    """后台清理不能删正在用的副本（2026-10-24 NAS 上任务 32 的副本在分析前被删掉，报 404）。"""
    task_id = await env.setup()

    async def cleanup_now():
        await env.cloud.cleanup("ark-test-key-1234")
        assert env.ark.deleted == [] and len(repo.list_cloud_files(env.w.conn)) == 1

    env.ark.on_analyze = cleanup_now
    await env.worker.process(task_id)
    assert env.task(task_id)["status"] == "completed" and env.task(task_id)["attempts"] == 0
    env.assert_cleaned(task_id)


async def test_failed_delete_left_for_cleanup(env: Env) -> None:
    """用完后删除失败：不再算正在使用，交给后台清理。"""
    task_id = await env.setup()
    env.ark.fail("delete", Retryable("方舟服务端错误 500"), Retryable("方舟服务端错误 500"))
    await env.worker.process(task_id)
    assert len(repo.list_cloud_files(env.w.conn)) == 1
    await env.cloud.cleanup("ark-test-key-1234")
    assert repo.list_cloud_files(env.w.conn) == [] and env.ark.files == {}


async def test_stopped_while_preparing_not_processed(env: Env, monkeypatch) -> None:
    """执行器取出任务、还没改成处理中时用户点了停止：不再上传和保存。"""
    task_id = await env.setup()
    real = env.worker._check_space

    async def stop_first(extra: int = 0) -> None:
        repo.mark_stopped(env.w.conn, task_id, env.clock.now_ms())
        await real(extra)

    monkeypatch.setattr(env.worker, "_check_space", stop_first)
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["status"] == "stopped" and task["current_video_path"] == VIDEO_IN
    assert env.ark.uploads == [] and env.w.files(".md") == []


async def test_ignored_while_preparing_not_deleted(env: Env, monkeypatch) -> None:
    """准备期间用户点了忽略、视频已移走：任务保持已忽略，不当成“视频被删除”删掉。"""
    task_id = await env.setup()
    real = env.worker._verify

    async def ignore_first(src_rel, expected):
        monkeypatch.setattr(env.worker, "_verify", real)
        await env.w.journal.ignore(task_id)
        return await real(src_rel, expected)

    monkeypatch.setattr(env.worker, "_verify", ignore_first)
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task is not None and task["status"] == "ignored"
    assert env.w.file(task["current_video_path"]).exists() and env.ark.uploads == []


# ---- “音频过短”：任务失败，不重试（立项 8.3）----

SHORT = "音频过短或无法解析：The request failed because the duration of the input audio (9.875ms) is shorter"


async def test_audio_too_short_fails_without_retry(env: Env) -> None:
    task_id = await env.setup()
    env.ark.fail("analyze", TaskFailed(SHORT))
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert (task["status"], task["fail_reason"], task["attempts"]) == ("failed", SHORT, 0)
    assert len(env.ark.uploads) == 1
    env.assert_cleaned(task_id)


# ---- 超过 512MB：只传音频；没有音轨或长于 120 分钟时忽略（立项第 3 节、4.3）----

IGNORED = "output/ignored/设计师小王&抖音&PS抠头发教程.mp4"


def make_oversized(env: Env, monkeypatch) -> None:
    monkeypatch.setattr(media, "MAX_UPLOAD_BYTES", env.w.file(VIDEO_IN).stat().st_size - 1)


async def test_oversized_audio_only(env: Env, monkeypatch) -> None:
    task_id = await env.setup()
    make_oversized(env, monkeypatch)
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert (task["status"], task["analysis_mode"], task["notice"]) == ("completed", "audio", None)
    assert env.ark.uploads == [env.w.file(f".vidlex/tmp/{task_id}/audio.m4a")] and env.ark.kinds == ["audio"]
    assert env.ark.prompts[0].startswith(AUDIO_ONLY_HINT + "\ntitle：")
    assert "超过 512MB，只提取音频" in env.logs(task_id)
    # 归档的是原视频，md 里嵌入视频
    assert task["current_video_path"] == f"output/video/{TITLE}.mp4"
    assert hashlib.sha256(env.w.file(task["current_video_path"]).read_bytes()).hexdigest() == env.source_hash
    assert f"![[{TITLE}.mp4]]" in env.w.file(MD).read_text()
    env.assert_cleaned(task_id)


async def test_oversized_too_long_ignored_without_upload(env: Env, monkeypatch) -> None:
    task_id = await env.setup()
    make_oversized(env, monkeypatch)
    monkeypatch.setattr(audio, "AUDIO_ONLY_MAX_SECONDS", 1)
    env.w.conn.execute("UPDATE settings SET api_key = NULL WHERE id = 1")  # 不需要 Key，也不会报“请先填写”
    assert await env.worker.process(task_id) is None
    task = env.task(task_id)
    assert (task["status"], task["current_video_path"], task["notice"]) == ("ignored", IGNORED, OVERSIZED_TOO_LONG)
    assert env.ark.uploads == [] and env.w.files(".md") == []
    state = env.queue()
    assert (state["queue_state"], state["consecutive_failures"]) == ("running", 0)
    assert OVERSIZED_TOO_LONG in env.logs(task_id)


async def test_oversized_without_audio_ignored(tmp_path: Path, samples, monkeypatch) -> None:
    env = Env(tmp_path, samples["silent.mp4"])
    task_id = await env.setup()
    make_oversized(env, monkeypatch)
    await env.worker.process(task_id)
    assert (env.task(task_id)["status"], env.task(task_id)["notice"]) == ("ignored", OVERSIZED_NO_AUDIO)


async def test_oversized_start_rechecks(env: Env, monkeypatch) -> None:
    task_id = await env.setup()
    make_oversized(env, monkeypatch)
    monkeypatch.setattr(audio, "AUDIO_ONLY_MAX_SECONDS", 1)
    await env.worker.process(task_id)
    await env.w.journal.unignore(task_id)  # 用户点“开始”
    await env.worker.process(task_id)
    assert env.task(task_id)["status"] == "ignored"  # 仍超过：再次忽略
    env.w.file(IGNORED).unlink()
    shutil.copy(env.sample, env.w.file(IGNORED))  # 用户换成了新文件（立项 8.6）
    monkeypatch.undo()  # 新文件不超过上限
    await env.w.journal.unignore(task_id)
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["status"] == "completed" and len(env.ark.uploads) == 1 and env.ark.kinds == ["video"]


async def test_oversized_ignore_failure_marks_failed(env: Env, monkeypatch) -> None:
    task_id = await env.setup()
    make_oversized(env, monkeypatch)
    monkeypatch.setattr(audio, "AUDIO_ONLY_MAX_SECONDS", 1)
    def move(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(env.w.lib, "move", move)
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["status"] == "failed"
    assert task["fail_reason"].startswith(f"{OVERSIZED_TOO_LONG}；自动忽略失败：剪切失败")
    assert task["current_video_path"] == VIDEO_IN


# ---- 8.4 返回内容检查 ----

async def test_invalid_json_retried(env: Env) -> None:
    task_id = await env.setup()
    env.ark.results = ["不是 JSON", "```json\n" + json.dumps(RESULT, ensure_ascii=False) + "\n```"]
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["status"] == "completed" and task["attempts"] == 1


async def test_empty_fields_fallbacks(env: Env) -> None:
    task_id = await env.setup()
    env.ark.results = [json.dumps({**RESULT, "title": "###", "highlight": "", "transcript": ["无语音。"]},
                                  ensure_ascii=False)]
    await env.worker.process(task_id)
    task = env.task(task_id)
    assert task["title"] == "PS抠头发教程"  # 标题兜底：原文件名里的标题名
    md = env.w.file(task["md_path"]).read_text()
    assert "## 视频亮点\n（无）" in md and "## 转写\n（无语音）" in md


# ---- 8.5 存储空间 ----

async def test_not_enough_space(env: Env, monkeypatch) -> None:
    task_id = await env.setup()
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**9, 10**9, 1024))
    await env.worker.process(task_id)
    assert env.task(task_id)["status"] == "queued"
    assert env.queue()["error_message"] == NO_SPACE
    assert env.ark.uploads == []


async def test_output_not_writable_then_resume(env: Env, monkeypatch) -> None:
    task_id = await env.setup()
    original = env.w.lib.move

    def move(src, dst):
        if dst.startswith("output/"):
            raise PermissionError(13, "Permission denied")
        return original(src, dst)

    monkeypatch.setattr(env.w.lib, "move", move)
    error = await env.worker.process(task_id)
    assert isinstance(error, GlobalError) and env.queue()["queue_state"] == "error"
    assert repo.get_op(env.w.conn, task_id)["kind"] == "commit"  # 结果已提交，不重新分析
    assert env.w.file(f".vidlex/tmp/{task_id}/note.md").exists()
    monkeypatch.undo()
    assert await env.w.start()  # 修好后“继续”：按提交记录完成
    assert env.task(task_id)["status"] == "completed"
    assert len(env.ark.uploads) == 1


# ---- 8.6 视频被删除或改动 ----

async def test_deleted_while_processing(env: Env) -> None:
    task_id = await env.setup()

    async def delete_source():
        env.w.file(VIDEO_IN).unlink()

    env.ark.on_analyze = delete_source
    await env.worker.process(task_id)
    assert env.task(task_id) is None
    assert env.w.files(".md") == []
    env.assert_cleaned(task_id)


async def test_replaced_while_processing(env: Env) -> None:
    task_id = await env.setup()

    async def replace_source():
        env.w.file(VIDEO_IN).unlink()
        env.w.file(VIDEO_IN).write_bytes(b"new video")

    env.ark.on_analyze = replace_source
    await env.worker.process(task_id)
    assert env.task(task_id) is None
    assert env.w.file(VIDEO_IN).read_bytes() == b"new video"  # 不移动替换后的新文件
    assert env.w.files(".md") == []
    env.assert_cleaned(task_id)


async def test_replaced_before_start(env: Env) -> None:
    task_id = await env.setup()
    env.w.file(VIDEO_IN).write_bytes(b"changed while queued")
    await env.worker.process(task_id)
    assert env.task(task_id) is None and env.ark.uploads == []


# ---- 停止（立项 4.3）----

async def test_cancel_cleans_up(env: Env) -> None:
    task_id = await env.setup()
    started = asyncio.Event()

    async def block():
        started.set()
        await asyncio.Event().wait()

    env.ark.on_analyze = block
    job = asyncio.create_task(env.worker.process(task_id))
    await asyncio.wait_for(started.wait(), 10)  # 出问题时报错，不卡死
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert env.task(task_id)["status"] == "processing"  # 状态由 runtime 决定
    env.assert_cleaned(task_id)
    assert env.w.file(VIDEO_IN).exists()


async def test_tmp_cleanup_during_transcode_keeps_current_task(tmp_path: Path, samples, monkeypatch) -> None:
    """扫描结束的例行清理恰好发生在转码刚完成、还没上传时：处理中任务的临时副本不能被删。"""
    env = Env(tmp_path, samples["eac3.mov"], "教程/a&b&c.mov")
    task_id = await env.setup()
    env.w.file(".vidlex/tmp/999").mkdir(parents=True)  # 没有对应任务的遗留目录
    scanner = Scanner(env.w.conn, env.w.lib, env.clock)
    real = media.transcode_audio

    async def transcode_then_housekeeping(src, dst):
        await real(src, dst)
        await scanner.housekeeping()
        assert dst.exists()

    monkeypatch.setattr(media, "transcode_audio", transcode_then_housekeeping)
    await env.worker.process(task_id)
    assert env.task(task_id)["status"] == "completed"
    assert not env.w.file(".vidlex/tmp/999").exists()  # 清理确实执行了
    env.assert_cleaned(task_id)
