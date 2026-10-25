"""ffprobe / ffmpeg 检查文件（立项第 3 节、8.3）。用 FFmpeg 现场生成的小视频。"""

import asyncio
import os
from pathlib import Path

import pytest

from app.domain.audio import Stream
from app.domain.errors import FILE_BROKEN, TRANSCODE_FAILED, TaskFailed
from app.services import media

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("name", ["lc.mp4", "eac3.mov"])
async def test_good_files_pass(samples: dict[str, Path], name: str) -> None:
    await media.check_readable(samples[name])


async def test_streams(samples: dict[str, Path]) -> None:
    info = await media.check_readable(samples["lc.mp4"])
    assert info.streams == [Stream("video", "h264", "High", False), Stream("audio", "aac", "LC", False)]
    assert info.duration == pytest.approx(2.0, abs=0.1)
    assert (await media.check_readable(samples["audio_only.mp4"])).streams == [Stream("audio", "mp3", None, False)]
    assert await media.probe(samples["garbage.mp4"]) is None


async def test_extract_audio(samples: dict[str, Path], tmp_path: Path) -> None:
    dst = tmp_path / "audio.m4a"
    await media.extract_audio(samples["lc.mp4"], dst)
    info = await media.check_readable(dst)
    assert info.streams == [Stream("audio", "aac", "LC", False)]
    with pytest.raises(TaskFailed, match=TRANSCODE_FAILED):
        await media.extract_audio(samples["silent.mp4"], tmp_path / "none.m4a")  # 没有音轨


async def test_transcode_audio(samples: dict[str, Path], tmp_path: Path) -> None:
    src, dst = samples["eac3.mov"], tmp_path / "upload.mov"
    before = src.read_bytes()
    await media.transcode_audio(src, dst)
    assert (await media.check_readable(dst)).streams == [
        Stream("video", "h264", "High", False), Stream("audio", "aac", "LC", False),
    ]
    assert src.read_bytes() == before


async def test_transcode_failure(samples: dict[str, Path], tmp_path: Path) -> None:
    with pytest.raises(TaskFailed, match=TRANSCODE_FAILED):
        await media.transcode_audio(samples["audio_only.mp4"], tmp_path / "upload.mp4")  # 没有视频流可映射


@pytest.mark.parametrize("name", ["garbage.mp4", "truncated.mp4"])
async def test_broken_files_fail(samples: dict[str, Path], name: str) -> None:
    with pytest.raises(TaskFailed, match=FILE_BROKEN):
        await media.check_readable(samples[name])


async def test_ffmpeg_version() -> None:
    assert (await media.ffmpeg_version()).startswith("ffmpeg version")


async def test_cancel_kills_process() -> None:
    pids: list[int] = []
    original = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await original(*args, **kwargs)
        pids.append(proc.pid)
        return proc

    asyncio.create_subprocess_exec = spy  # type: ignore[assignment]
    try:
        task = asyncio.create_task(media._run(["sleep", "30"]))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        asyncio.create_subprocess_exec = original  # type: ignore[assignment]
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
