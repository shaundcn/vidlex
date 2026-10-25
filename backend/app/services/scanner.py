"""扫描 input、检查已忽略的文件、清理临时目录（立项第 3 节、8.6，技术选型 13.6）。

遍历在线程里做，结果回到主线程按数据库的最新状态逐条复核后才写入。
"""

import asyncio
import logging
import os
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.db import repo
from app.domain import scanrules, stability
from app.domain.errors import classify_os_error
from app.domain.stability import Ident, Seen
from app.services import tasklog
from app.services.clock import Clock
from app.services.fsops import Library

log = logging.getLogger("vidlex.scanner")

# 这几种状态的任务，视频在 input 里；消失或被替换时由扫描器处理（处理中的由 worker 自己检查）
_IN_INPUT = frozenset({"queued", "failed", "stopped"})


@dataclass(frozen=True)
class ScanResult:
    new_files: bool  # 出现了上次没见过的文件（用于唤醒休眠）
    pending: bool  # 还有等待稳定的文件
    enqueued: int


@dataclass
class _Walk:
    files: dict[str, Ident] = field(default_factory=dict)
    skipped_dirs: list[str] = field(default_factory=list)  # 读不了的子目录：里面的任务不动
    bad_names: list[str] = field(default_factory=list)
    ignored_missing: set[str] = field(default_factory=set)


class Scanner:
    def __init__(self, conn: sqlite3.Connection, library: Library, clock: Clock) -> None:
        self.conn = conn
        self.lib = library
        self.clock = clock
        self.state: dict[str, Seen] = {}
        self._logged_bad: set[str] = set()

    def has_pending(self) -> bool:
        return stability.has_pending(self.state)

    async def scan(self) -> ScanResult:
        """input 本身读不了时抛 GlobalError（存储故障），不把“读不到”当成“文件被删了”。"""
        ignored = [t["current_video_path"] for t in repo.list_tasks(self.conn) if t["status"] == "ignored"]
        try:
            walk = await asyncio.to_thread(self._walk, ignored)
        except OSError as exc:
            raise classify_os_error(exc) from exc
        for name in walk.bad_names:
            if name not in self._logged_bad:
                self._logged_bad.add(name)
                log.warning("文件名不是合法 UTF-8，已跳过：%r", name)
        return self._apply(walk)

    async def housekeeping(self) -> None:
        """删除 30 天前的日志；清理不属于处理中任务、也不属于进度记录的临时目录。"""
        tasklog.cleanup(self.conn, self.clock.now_ms())
        keep = {str(t["id"]) for t in repo.list_tasks(self.conn) if t["status"] == "processing"}
        keep |= {str(op["task_id"]) for op in repo.list_ops(self.conn)}
        try:
            await asyncio.to_thread(self.lib.clean_tmp, keep)
        except OSError as exc:
            log.warning("清理临时目录失败：%s", exc)

    # ---- 线程里 ----

    def _walk(self, ignored_paths: list[str]) -> _Walk:
        walk = _Walk()
        root = self.lib.path("input")
        os.listdir(root)  # input 本身读不了：抛出，按存储故障处理

        def on_error(exc: OSError) -> None:
            walk.skipped_dirs.append(self._rel(exc.filename))
            log.warning("读取目录失败，本次跳过：%s（%s）", exc.filename, exc)

        for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
            for name in dirnames:
                if not scanrules.is_valid_name(name):
                    walk.bad_names.append(os.path.join(dirpath, name))
            dirnames[:] = [d for d in dirnames if scanrules.should_descend(d)]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if not scanrules.is_valid_name(name):
                    walk.bad_names.append(full)
                    continue
                if not scanrules.is_video_file(name):
                    continue
                try:
                    st = os.stat(full, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    walk.files[self._rel(full)] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        walk.ignored_missing = {p for p in ignored_paths if not self.lib.exists(p)}
        return walk

    def _rel(self, full: str) -> str:
        return PurePosixPath(os.path.relpath(full, self.lib.root)).as_posix()

    # ---- 主线程 ----

    def _apply(self, walk: _Walk) -> ScanResult:
        tasks = repo.list_tasks(self.conn)
        with_op = {op["task_id"] for op in repo.list_ops(self.conn)}
        untracked = self._untracked(walk, tasks, with_op)
        self._drop_missing(walk, tasks, with_op)
        new_files = any(p not in self.state for p in untracked)
        self.state, stable = stability.observe(self.state, untracked)
        return ScanResult(new_files, self.has_pending(), self._enqueue(stable, untracked))

    def _untracked(self, walk: _Walk, tasks: list[sqlite3.Row], with_op: set[int]) -> dict[str, Ident]:
        """扫到的文件里没有对应任务的；视频被替换的任务删除后，新文件也算（立项 8.6）。"""
        by_path = {t["current_video_path"]: t for t in tasks if t["current_video_path"]}
        untracked: dict[str, Ident] = {}
        for path, ident in walk.files.items():
            task = by_path.get(path)
            if task is None:
                untracked[path] = ident
            elif task["id"] not in with_op and task["status"] in _IN_INPUT and ident != repo.row_ident(task):
                if self._drop(task, "视频被替换或改动，按新视频重新等待稳定"):
                    untracked[path] = ident
                    self.state.pop(path, None)
        return untracked

    def _drop_missing(self, walk: _Walk, tasks: list[sqlite3.Row], with_op: set[int]) -> None:
        """视频已不在的任务删除；有文件操作记录的任务交给记录恢复，不动。"""
        for task in tasks:
            path = task["current_video_path"]
            if task["id"] in with_op or path is None:
                continue
            if task["status"] in _IN_INPUT and _missing_from_input(path, walk):
                self._drop(task, "视频已被删除或移走")
            elif task["status"] == "ignored" and path in walk.ignored_missing:
                self._drop(task, "已忽略的视频已被删除或移走")

    def _enqueue(self, stable: list[str], untracked: dict[str, Ident]) -> int:
        """稳定的新文件加入队列，返回加入的个数；路径已被任务或记录占用的跳过。"""
        enqueued = 0
        for path in stable:
            if repo.path_taken(self.conn, path):
                continue
            orig = path.removeprefix("input/")
            task_id = repo.enqueue_new_task(self.conn, orig, path, untracked[path], self.clock.now_ms())
            tasklog.write_task(self.conn, self.clock.now_ms(), task_id, "info", "新视频加入队列")
            enqueued += 1
        return enqueued

    def _drop(self, task: sqlite3.Row, reason: str) -> bool:
        """按数据库最新状态复核后删除任务；遍历期间任务已变化就不动。"""
        current = repo.get_task(self.conn, task["id"])
        if (current is None or current["status"] != task["status"]
                or current["current_video_path"] != task["current_video_path"]
                or repo.get_op(self.conn, task["id"]) is not None):
            return False
        tasklog.write_task(self.conn, self.clock.now_ms(), task["id"], "warning", f"任务已删除：{reason}")
        repo.remove_task(self.conn, task["id"])
        return True


def _missing_from_input(path: str, walk: _Walk) -> bool:
    """在 input 里、这次没扫到，也不在读不了的子目录里。"""
    return (path.startswith("input/") and path not in walk.files
            and not any(_under(path, d) for d in walk.skipped_dirs))


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip("/") + "/")
