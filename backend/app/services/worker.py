"""处理一个任务（立项第 3 节、8.1、8.3–8.6，技术选型 13.7、13.10）。

process() 自己处理所有结果：完成、失败、视频消失、影响所有任务的错误。
被取消（点“停止”或关机）时只做清理，状态由 runtime 决定。
"""

import asyncio
import logging
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from app.db import repo
from app.domain import audio, naming, status
from app.domain.errors import (
    ATTEMPT_TIME_LIMIT, NETWORK_RETRY_DELAYS, NO_API_KEY, NO_SPACE, OVERSIZED_NO_AUDIO,
    TRANSCODED_OVERSIZED, Conflict, GlobalError, NetworkError, NotFound, RateLimited, Retryable,
    TaskFailed, classify_os_error, network_down, retry_delay,
)
from app.domain.mdrender import render_md
from app.domain.prompt import AUDIO_ONLY_HINT, build_prompt
from app.domain.result import AnalysisResult, parse_result
from app.domain.stability import Ident
from app.services import media, tasklog
from app.services.ark_client import ANSWER_EARLIER_S, FILE_TTL_S, ArkApi
from app.services.clock import Clock
from app.services.cloud import Cloud
from app.services.fsops import TMP_DIR, Library
from app.services.journal import Journal

log = logging.getLogger("vidlex.worker")

POLL_INTERVAL_S = 5
PREPROCESS_TIMEOUT_S = 360  # 方舟预处理限时 5 分钟，留 1 分钟余量（推导，未实测）
CLEANUP_LIMIT_S = 30
LIBRARY_MIN_FREE = 100 * 1024 * 1024
AUDIO_ONLY_MAX_BYTES = 64 * 1024 * 1024  # 只传音频时临时音轨的空间预留（120 分钟 64k 约 58MB）
DATA_MIN_FREE = 50 * 1024 * 1024


class SourceGone(Exception):
    """源视频被删除、移走或替换（立项 8.6）。"""


class Worker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        library: Library,
        journal: Journal,
        cloud: Cloud,
        ark: ArkApi,
        clock: Clock,
        data_dir: Path,
    ) -> None:
        self.conn = conn
        self.lib = library
        self.journal = journal
        self.cloud = cloud
        self.ark = ark
        self.clock = clock
        self.data_dir = data_dir
        self.shutting_down = False

    async def process(self, task_id: int) -> GlobalError | None:
        """处理一个排队中的任务。返回影响所有任务的错误（队列已进入出错），否则 None。"""
        task = repo.get_task(self.conn, task_id)
        if task is None or task["status"] != "queued":
            return None
        start_attempts = task["attempts"]
        api_key = repo.get_settings(self.conn)["api_key"]
        uploaded: list[str] = []
        try:
            await self._process(task, api_key, uploaded)
        except GlobalError as exc:
            self._global_error(task_id, start_attempts, exc)
            return exc
        except OSError as exc:
            error = classify_os_error(exc)
            self._global_error(task_id, start_attempts, error)
            return error
        except TaskFailed as exc:
            self._fail(task_id, str(exc))
        except SourceGone as exc:
            self._source_gone(task_id, str(exc))
        finally:
            await self._cleanup(task_id, api_key, uploaded)
        return None

    # ---- 流程 ----

    async def _process(self, task: sqlite3.Row, api_key: str | None, uploaded: list[str]) -> None:
        task_id, src_rel = task["id"], task["current_video_path"]
        snap = await self._verify(src_rel, repo.row_ident(task))
        # 超过 512MB 只传音频；没有音轨或长于 120 分钟时在开始处理前忽略（立项第 3 节）
        audio_only = snap[2] > media.MAX_UPLOAD_BYTES
        if audio_only and (reason := await self._oversized_reason(src_rel)) is not None:
            await self._ignore_oversized(task_id, reason)
            return
        if not api_key:
            raise GlobalError(NO_API_KEY)
        await self._check_space()
        prompt = self._prompt(audio_only)
        mode: audio.AnalysisMode = "audio" if audio_only else "video"
        if not repo.start_processing(self.conn, task_id, "upload", snap, mode, self.clock.now_ms()):
            return  # 准备期间用户点了停止或忽略：任务已不是排队中，不再处理
        self._log(task_id, "info", f"开始处理。提示词：\n{prompt}")

        upload_path, kind = await self._upload_file(task_id, src_rel, snap, audio_only)
        result = await self._analyze(task, snap, api_key, upload_path, prompt, kind, uploaded)
        await self._save(task, snap, result)

    async def _oversized_reason(self, src_rel: str) -> str | None:
        """超过 512MB：读出音轨和时长，返回忽略原因；可以只传音频时返回 None。"""
        info = await media.probe(self.lib.path(src_rel))  # 读不了时交给后面的完整检查报错
        return audio.oversized_ignore_reason(info) if info is not None else None

    def _prompt(self, audio_only: bool) -> str:
        prompt = build_prompt(repo.prompts_of(repo.get_settings(self.conn)))
        return f"{AUDIO_ONLY_HINT}\n{prompt}" if audio_only else prompt

    async def _upload_file(
        self, task_id: int, src_rel: str, snap: Ident, audio_only: bool
    ) -> tuple[Path, audio.AnalysisMode]:
        """检查能完整读取，返回要上传的文件（提取的音轨、转码副本或原视频）和分析方式。"""
        info = await media.check_readable(self.lib.path(src_rel))
        tmp = f"{TMP_DIR}/{task_id}"
        if audio_only:
            return await self._extract_audio(task_id, info, src_rel, f"{tmp}/audio.m4a"), "audio"
        ext = PurePosixPath(src_rel).suffix
        return await self._transcode_if_needed(task_id, info.streams, src_rel, snap, f"{tmp}/upload{ext}"), "video"

    async def _analyze(
        self, task: sqlite3.Row, snap: Ident, api_key: str, upload_path: Path, prompt: str, kind: audio.AnalysisMode,
        uploaded: list[str],
    ) -> AnalysisResult:
        """可重试的错误按立项 8.1 等待后再试，次数用完时任务失败。"""
        task_id, src_rel, attempts = task["id"], task["current_video_path"], task["attempts"]
        while True:
            try:
                await self._verify(src_rel, snap)  # 上传前复核
                return parse_result(await self._attempt(task_id, api_key, upload_path, prompt, uploaded, kind))
            except Retryable as exc:
                error = exc
            attempts += 1
            repo.set_attempts(self.conn, task_id, attempts, self.clock.now_ms())
            delay = retry_delay(attempts)
            if delay is None:
                raise TaskFailed(str(error))
            self._log(task_id, "warning", f"第 {attempts} 次尝试失败：{error}；"
                                          f"第 {attempts + 1} 次尝试将在 {delay // 60} 分钟后开始")
            await self.clock.sleep(delay)

    async def _save(self, task: sqlite3.Row, snap: Ident, result: AnalysisResult) -> None:
        """生成 md，复核源视频后写提交记录并执行（技术选型 13.8）。"""
        task_id, src_rel = task["id"], task["current_video_path"]
        repo.set_step(self.conn, task_id, "render_md", self.clock.now_ms())
        filename = naming.original_filename(task["orig_rel_path"])
        title = naming.fallback_title(result.title, filename, task_id)
        source = naming.split_source(filename)
        paths = self.journal.pick_commit_paths(title, PurePosixPath(src_rel).suffix)
        md = render_md(title=title, platform=source.platform, author=source.author,
                       original_file=task["orig_rel_path"], video_name=paths.video_name, result=result)
        md_tmp = f"{TMP_DIR}/{task_id}/note.md"
        await asyncio.to_thread(self.lib.write_atomic, md_tmp, md)
        await self._verify(src_rel, snap)  # 结果提交前复核
        repo.begin_commit(self.conn, task_id, src_rel, snap, paths.video_path, md_tmp, paths.md_path,
                          title, self.clock.now_ms())
        self._log(task_id, "info", "保存结果")
        await self.journal.run_commit(task_id)

    async def _transcode_if_needed(
        self, task_id: int, streams: list[audio.Stream], src_rel: str, snap: Ident, dst_rel: str
    ) -> Path:
        """方舟认不准的音频先转成 AAC-LC，上传临时副本；原视频不动（技术选型 11.5）。返回要上传的文件。"""
        stream = audio.needs_transcode(streams)
        if stream is None:
            return self.lib.path(src_rel)
        self._log(task_id, "info", f"音频为 {audio.describe(stream)}，转成 AAC-LC 后上传")
        return await self._transcode(task_id, src_rel, snap, dst_rel)

    async def _transcode(self, task_id: int, src_rel: str, snap: Ident, dst_rel: str) -> Path:
        """生成“画面原样、音频 AAC-LC”的临时副本，返回副本路径。"""
        await self._check_space(extra=snap[2])  # 临时副本约等于原视频大小
        repo.set_step(self.conn, task_id, "transcode", self.clock.now_ms())
        dst = await self._tmp_path(dst_rel)
        await media.transcode_audio(self.lib.path(src_rel), dst)
        if (await asyncio.to_thread(dst.stat)).st_size > media.MAX_UPLOAD_BYTES:
            raise TaskFailed(TRANSCODED_OVERSIZED)
        return dst

    async def _extract_audio(self, task_id: int, info: audio.Probe, src_rel: str, dst_rel: str) -> Path:
        """超过 512MB：只提取音轨上传（立项第 3 节）。没有画面的文件仍按“不是视频”失败。"""
        audio.require_video(info.streams)
        if audio.first_audio(info.streams) is None:
            raise TaskFailed(OVERSIZED_NO_AUDIO)  # 开始前没读出编码时才会走到这里
        await self._check_space(extra=AUDIO_ONLY_MAX_BYTES)
        repo.set_step(self.conn, task_id, "transcode", self.clock.now_ms())
        self._log(task_id, "info", "超过 512MB，只提取音频（AAC-LC 单声道）上传")
        dst = await self._tmp_path(dst_rel)
        await media.extract_audio(self.lib.path(src_rel), dst)
        return dst

    async def _tmp_path(self, rel: str) -> Path:
        await asyncio.to_thread(self.lib.make_dirs, str(PurePosixPath(rel).parent))
        return self.lib.path(rel)

    async def _attempt(
        self, task_id: int, api_key: str, upload_path: Path, prompt: str, uploaded: list[str], kind: audio.AnalysisMode
    ) -> str:
        """一次尝试：上传、预处理、分析合计限时 60 分钟，限流等待也算在内（立项 8.1）。"""
        file_id: str | None = None
        try:
            async with self.clock.limit(ATTEMPT_TIME_LIMIT):
                repo.set_step(self.conn, task_id, "upload", self.clock.now_ms())
                file_expire = self.clock.now_ms() // 1000 + FILE_TTL_S
                file_id = await self._call(self.ark.upload, api_key, upload_path, file_expire)
                self.cloud.register(file_id, file_expire)
                uploaded.append(file_id)
                started = self.clock.now_ms()
                while (state := await self._call(self.ark.file_status, api_key, file_id)) != "active":
                    if state == "failed":
                        raise Retryable("视频预处理失败")
                    if self.clock.now_ms() - started > PREPROCESS_TIMEOUT_S * 1000:
                        raise Retryable("视频预处理超时")
                    await self.clock.sleep(POLL_INTERVAL_S)
                repo.set_step(self.conn, task_id, "analyze", self.clock.now_ms())
                return await self._call(self.ark.analyze, api_key, file_id, prompt, file_expire - ANSWER_EARLIER_S,
                                        kind)
        except TimeoutError:
            raise Retryable("单次尝试超过 60 分钟") from None
        finally:
            if file_id is not None and not self.shutting_down:
                await self._delete_cloud(api_key, file_id, uploaded)

    async def _call[T](self, fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        """限流：按要求等待后重试，不消耗尝试次数；断网：等 10、30、60 秒，仍不通就影响所有任务。"""
        network_failures = 0
        while True:
            try:
                return await fn(*args)
            except RateLimited as exc:
                log.info("方舟限流，%d 秒后重试", exc.retry_after)
                await self.clock.sleep(exc.retry_after)
            except NetworkError as exc:
                if network_failures == len(NETWORK_RETRY_DELAYS):
                    raise network_down() from exc
                await self.clock.sleep(NETWORK_RETRY_DELAYS[network_failures])
                network_failures += 1

    async def _verify(self, src_rel: str, expected: Ident) -> Ident:
        ident = await asyncio.to_thread(self.lib.ident, src_rel)
        if ident is None:
            raise SourceGone("视频已被删除或移走")
        if ident != expected:
            raise SourceGone("视频被替换或改动，按新视频重新等待稳定")
        return ident

    async def _check_space(self, extra: int = 0) -> None:
        """每个任务开始前检查剩余空间（立项 8.5）；要转码时再查一次，多留出临时副本的大小。"""
        library_free = (await asyncio.to_thread(shutil.disk_usage, self.lib.root)).free
        data_free = (await asyncio.to_thread(shutil.disk_usage, self.data_dir)).free
        if library_free < LIBRARY_MIN_FREE + extra or data_free < DATA_MIN_FREE:
            raise GlobalError(NO_SPACE)

    async def _ignore_oversized(self, task_id: int, reason: str) -> None:
        """超过 512MB 且没有音轨或长于 120 分钟：剪切到 output/ignored/，不上传、不计入连续失败（立项第 3 节）。

        剪切失败时标为失败：留在排队中的话执行器会立刻又取到它。
        """
        task = repo.get_task(self.conn, task_id)
        if task is None or task["status"] != "queued":
            return  # 探测期间用户点了停止或忽略
        try:
            await self.journal.ignore(task_id, notice=reason)
        except NotFound:
            return  # 剪切时视频已被删除：任务已删除并写了日志
        except Conflict as exc:
            task = repo.get_task(self.conn, task_id)
            if task is not None and task["status"] == "queued":
                repo.start_processing(self.conn, task_id, "upload", repo.row_ident(task), "audio", self.clock.now_ms())
                self._fail(task_id, f"{reason}；自动忽略失败：{exc}")
            return
        self._log(task_id, "info", reason)

    # ---- 结果 ----

    def _global_error(self, task_id: int, start_attempts: int, exc: GlobalError) -> None:
        """影响所有任务：退回排队最前，本轮尝试不计入，不计入连续失败（立项 8.1）。"""
        state = repo.get_runtime_state(self.conn)
        change = status.on_global_error(state["queue_state"], bool(state["error_auto_resume"]),
                                        str(exc), exc.auto_resume)
        queue = (change.state, change.error_message, change.auto_resume)
        task = repo.get_task(self.conn, task_id)
        if task is not None and task["status"] == "processing" and repo.get_op(self.conn, task_id) is None:
            repo.requeue_front(self.conn, task_id, start_attempts, self.clock.now_ms(), queue)
        else:
            # 还没开始处理，或已进入保存结果（按提交记录在“继续”时恢复）
            repo.set_queue_state(self.conn, *queue, False, self.clock.now_ms())
        self._log(task_id, "error", f"影响所有任务的问题，队列暂停：{exc}")

    def _source_gone(self, task_id: int, reason: str) -> None:
        """视频消失或被替换：删除任务（立项 8.6）。

        只删仍由这里处理的任务：准备期间被用户忽略时视频已移走，任务已是“已忽略”，不能删。
        """
        task = repo.get_task(self.conn, task_id)
        if (task is not None and task["status"] in ("queued", "processing")
                and repo.get_op(self.conn, task_id) is None):
            self._log(task_id, "warning", f"任务已删除：{reason}")
            repo.remove_task(self.conn, task_id)

    def _fail(self, task_id: int, reason: str) -> None:
        task = repo.get_task(self.conn, task_id)
        if task is None or task["status"] != "processing" or repo.get_op(self.conn, task_id) is not None:
            return
        state = repo.get_runtime_state(self.conn)
        failures, change = status.after_task_result(
            True, state["consecutive_failures"], state["queue_state"], bool(state["error_auto_resume"])
        )
        queue = (change.state, change.error_message, change.auto_resume) if change else None
        repo.mark_failed(self.conn, task_id, reason, task["attempts"], failures, queue, self.clock.now_ms())
        self._log(task_id, "error", f"处理失败：{reason}")

    async def _cleanup(self, task_id: int, api_key: str | None, uploaded: list[str]) -> None:
        """删除云端副本和临时目录。关机时不联网，留给下次启动的清理；commit 记录引用的 md 临时文件保留。"""
        if not self.shutting_down:
            for file_id in list(uploaded):
                await self._delete_cloud(api_key, file_id, uploaded)
        op = repo.get_op(self.conn, task_id)
        if op is None or op["kind"] != "commit":
            await asyncio.to_thread(self.lib.remove_tree, f"{TMP_DIR}/{task_id}")

    async def _delete_cloud(self, api_key: str | None, file_id: str, uploaded: list[str]) -> None:
        try:
            async with self.clock.limit(CLEANUP_LIMIT_S):
                deleted = await self.cloud.delete(api_key, file_id)
        except TimeoutError:
            deleted = False
        if deleted:
            uploaded.remove(file_id)

    def _log(self, task_id: int, level: str, message: str) -> None:
        tasklog.write_task(self.conn, self.clock.now_ms(), task_id, level, message)
