"""阶段 3 测试共用的“视频库 + 数据库”环境，以及模拟断电的工具。"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.db import connection, repo
from app.domain.prompt import DEFAULT_PROMPTS
from app.services import startup
from app.services.clock import Clock
from app.services.fsops import Library
from app.services.journal import Journal

ORIG = "教程/设计师小王&抖音&PS抠头发教程.mp4"
TITLE = "3 步用 PS 快速抠出发丝"


class World:
    def __init__(self, root: Path, clock: Clock | None = None) -> None:
        self.root = root
        self.lib_dir = root / "library"
        self.lib_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "data" / "vidlex.db"
        self.clock = clock or Clock()
        self.open()

    def open(self) -> None:
        self.conn = connection.open_database(self.db_path)
        repo.ensure_singletons(self.conn, DEFAULT_PROMPTS, 1)
        self.lib = Library(self.lib_dir)
        self.journal = Journal(self.conn, self.lib, self.clock)

    async def start(self) -> bool:
        return await startup.init_library(self.conn, self.lib, self.journal, self.clock) is None

    async def restart(self) -> bool:
        """模拟断电后重启：关闭连接（没提交的事务丢失），重新打开并走启动流程。"""
        self.conn.close()
        self.open()
        return await self.start()

    def file(self, rel: str) -> Path:
        return self.lib_dir / rel

    def add_video(self, rel: str = ORIG, content: bytes = b"video-bytes") -> str:
        path = self.file(f"input/{rel}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return f"input/{rel}"

    def enqueue(self, rel: str = ORIG) -> int:
        video = self.add_video(rel) if not self.file(f"input/{rel}").exists() else f"input/{rel}"
        return repo.enqueue_new_task(self.conn, rel, video, self.lib.ident(video), 1)

    def set_processing(self, task_id: int, step: str = "render_md", attempts: int = 0) -> None:
        self.conn.execute(
            "UPDATE tasks SET status = 'processing', step = ?, queue_seq = NULL, attempts = ? WHERE id = ?",
            (step, attempts, task_id),
        )

    def enter_commit(self, task_id: int, title: str = TITLE) -> None:
        """worker 在阶段 3b 做的事：写 md 临时文件、选目标、写 commit 记录。"""
        task = repo.get_task(self.conn, task_id)
        ext = Path(task["current_video_path"]).suffix
        paths = self.journal.pick_commit_paths(task["orig_rel_path"], title, ext)
        tmp = f".vidlex/tmp/{task_id}/note.md"
        self.lib.write_atomic(tmp, f"![[{paths.video_name}]]\n")
        repo.begin_commit(self.conn, task_id, task["current_video_path"], self.lib.ident(task["current_video_path"]),
                          paths.video_path, tmp, paths.md_path, title, 2)

    def task(self, task_id: int) -> sqlite3.Row | None:
        return repo.get_task(self.conn, task_id)

    def files(self, suffix: str = "") -> list[str]:
        """视频库里除 .vidlex 外的全部文件（相对路径）。"""
        found = []
        for path in self.lib_dir.rglob("*"):
            rel = path.relative_to(self.lib_dir).as_posix()
            if path.is_file() and not rel.startswith(".vidlex") and rel.endswith(suffix):
                found.append(rel)
        return sorted(found)

    def tmp_entries(self) -> list[str]:
        tmp = self.file(".vidlex/tmp")
        return sorted(p.name for p in tmp.iterdir()) if tmp.exists() else []


class PowerLoss(BaseException):
    """模拟断电：继承 BaseException，业务代码里的 except Exception / OSError 都接不住。"""


class Crasher:
    """第 k 个持久化动作（link、unlink、rename、replace、数据库事务提交）完成后断电。"""

    def __init__(self, k: int) -> None:
        self.k = k
        self.count = 0

    def tick(self) -> None:
        self.count += 1
        if self.count == self.k:
            raise PowerLoss


@contextmanager
def power_loss_after(monkeypatch: pytest.MonkeyPatch, k: int) -> Iterator[Crasher]:
    crasher = Crasher(k)
    with monkeypatch.context() as m:
        for name in ("link", "unlink", "rename", "replace"):
            original = getattr(os, name)

            def wrapper(*args, _original=original, **kwargs):
                result = _original(*args, **kwargs)
                crasher.tick()
                return result

            m.setattr(os, name, wrapper)
        original_write = repo._write

        @contextmanager
        def write(conn: sqlite3.Connection, tasks: bool = True) -> Iterator[None]:
            with original_write(conn, tasks):
                yield
            crasher.tick()

        m.setattr(repo, "_write", write)
        yield crasher
