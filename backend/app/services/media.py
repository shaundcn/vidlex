"""调用 ffprobe 和 ffmpeg 检查文件、转码音频（立项第 3 节、8.3）。画面不转码、不压缩。

一律用参数列表调用，不经过 shell；任务被取消（点“停止”或关机）时杀掉子进程。
"""

import asyncio
import json
from pathlib import Path

from app.domain.audio import Probe, Stream
from app.domain.errors import FILE_BROKEN, TRANSCODE_FAILED, TaskFailed

MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 方舟单文件上限；超过的视频自动忽略


async def ffmpeg_version() -> str | None:
    """返回 `ffmpeg -version` 的第一行；找不到 ffmpeg 时返回 None。"""
    try:
        code, out, _ = await _run(["ffmpeg", "-version"])
    except FileNotFoundError:
        return None
    return out.splitlines()[0] if code == 0 and out else None


async def probe(path: Path) -> Probe | None:
    """ffprobe 读各条流的编码和总时长；读不了时返回 None（不判断文件是否完整）。"""
    code, out, _ = await _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,codec_name,profile:stream_disposition=attached_pic",
        "-of", "json", str(path),
    ])
    if code != 0:
        return None
    try:
        return _parse_probe(out)
    except ValueError:
        return None


async def check_readable(path: Path) -> Probe:
    """ffprobe 能读，并且从结尾读 2 秒不报错（复制中断的文件开头能读、结尾读不了）。返回编码和时长。

    读不了时任务失败“文件不完整或已损坏”，不上传、不重试（立项 8.3）。
    """
    info = await probe(path)
    if info is None:
        raise TaskFailed(FILE_BROKEN)
    code, _, err = await _run(
        ["ffmpeg", "-nostdin", "-v", "error", "-sseof", "-2", "-i", str(path), "-f", "null", "-"]
    )
    if code != 0 or err.strip():
        raise TaskFailed(FILE_BROKEN)
    return info


async def transcode_audio(src: Path, dst: Path) -> None:
    """只保留第一条视频和第一条音频：画面原样复制，音频转成 AAC-LC（技术选型 11.5）。失败不重试。"""
    code, _, _ = await _run([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", str(dst),
    ])
    if code != 0:
        raise TaskFailed(TRANSCODE_FAILED)


async def extract_audio(src: Path, dst: Path) -> None:
    """超过 512MB 时只上传音频：第一条音轨转成 AAC-LC 单声道 64k 的 m4a（2 小时约 58MB）。失败不重试。"""
    code, _, _ = await _run([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
        "-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(dst),
    ])
    if code != 0:
        raise TaskFailed(TRANSCODE_FAILED)


def _parse_probe(out: str) -> Probe:
    data = json.loads(out)
    try:
        duration: float | None = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return Probe(streams=_parse_streams(data.get("streams", [])), duration=duration)


def _parse_streams(raw: list[dict]) -> list[Stream]:
    return [
        Stream(
            codec_type=s.get("codec_type", ""),
            codec_name=s.get("codec_name", ""),
            profile=None if s.get("profile") in (None, "unknown") else s["profile"],
            attached_pic=bool(s.get("disposition", {}).get("attached_pic")),
        )
        for s in raw
    ]


async def _run(args: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
