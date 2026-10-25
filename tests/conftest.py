import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import Config
from app.db import connection, repo
from app.domain.prompt import DEFAULT_PROMPTS


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connection.open_database(tmp_path / "vidlex.db")
    repo.ensure_singletons(conn, DEFAULT_PROMPTS, 1)
    yield conn
    conn.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    library = tmp_path / "library"
    library.mkdir()
    return Config(data_dir=tmp_path / "data", library_dir=library)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ffmpeg(*args: str) -> None:
    import subprocess

    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """FFmpeg 现场生成的小视频（技术选型 13.14）。E-AC3 用来测“方舟认不准的音频先转码”。"""
    d = tmp_path_factory.mktemp("samples")
    video = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2"]
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    h264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest"]
    names = ("lc.mp4", "eac3.mov", "audio_only.mp4", "silent.mp4", "garbage.mp4", "truncated.mp4")
    out = {name: d / name for name in names}
    _ffmpeg(*video, *audio, *h264, "-c:a", "aac", "-movflags", "+faststart", str(out["lc.mp4"]))
    _ffmpeg(*video, *audio, *h264, "-c:a", "eac3", str(out["eac3.mov"]))
    _ffmpeg(*audio, "-c:a", "mp3", "-f", "mp3", str(out["audio_only.mp4"]))  # 扩展名是 .mp4 的 mp3
    _ffmpeg(*video, *h264, str(out["silent.mp4"]))  # 没有音轨
    out["garbage.mp4"].write_bytes(b"not a video" * 1000)
    data = out["lc.mp4"].read_bytes()
    out["truncated.mp4"].write_bytes(data[: len(data) * 6 // 10])  # 复制中断：开头能读、结尾缺一截
    return out
