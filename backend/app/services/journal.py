"""4 种文件操作记录的执行和恢复（立项 4.3、8.2、8.6，技术选型 4.7、13.8）。

先写记录，再动文件；每一步都可以重复执行，正常执行和断电后恢复走同一段代码：
先看目标位置是不是已经有身份相同的文件，再看源文件。
"""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Literal, NoReturn

from app.db import repo
from app.domain import naming
from app.domain.errors import STORAGE_FAULT, Conflict, GlobalError, NotFound, classify_os_error
from app.domain.stability import Ident
from app.domain.status import OpKind
from app.services import tasklog
from app.services.clock import Clock
from app.services.fsops import Library

Outcome = Literal["moved", "replaced", "missing"]

NOTICE_MISSING = "文件不存在"
NOTICE_CHANGED = "文件已变化，请等待重新扫描"
NOTICE_CHANGED_RETRY = "文件已变化，请重新点开始"
NOTICE_TARGET_TAKEN = "目标位置已有同名文件，请重试"


class Journal:
    def __init__(self, conn: sqlite3.Connection, library: Library, clock: Clock) -> None:
        self.conn = conn
        self.lib = library
        self.clock = clock
        # (种类, 阶段) → 这一阶段要做的事；取值和数据库 task_ops 的 CHECK 约束一致
        self._steps: dict[tuple[str, str], Callable[[sqlite3.Row], Awaitable[None]]] = {
            ("commit", "recorded"): self._commit_recorded,
            ("commit", "md_placed"): self._commit_md_placed,
            ("commit", "video_moved"): self._commit_video_moved,
            ("redo", "recorded"): self._redo_recorded,
            ("redo", "video_restored"): self._redo_video_restored,
            ("redo", "old_md_deleted"): self._redo_old_md_deleted,
            ("ignore", "recorded"): self._ignore_recorded,
            ("ignore", "video_moved"): self._ignore_video_moved,
            ("unignore", "recorded"): self._unignore_recorded,
            ("unignore", "video_restored"): self._unignore_video_restored,
        }

    # ---- 开始一个操作（调用方已用 domain.status 判断过能不能做）----

    async def ignore(self, task_id: int, notice: str | None = None) -> sqlite3.Row:
        """notice：完成后写进操作提示（超过 512MB 自动忽略时说明原因）。"""
        task = self._task(task_id)
        ident = await self._snapshot(task)
        if ident != repo.row_ident(task):
            # 入队后文件被替换：交给扫描器按新视频处理，不把新文件挂在旧任务下
            self._refuse(task, NOTICE_CHANGED)
        target = self._pick(lambda n: (naming.ignore_path(task["orig_rel_path"], n),))[0]
        result = await self._begin(task, "ignore", ident, target, None)
        if notice is not None and result["status"] == "ignored":
            repo.set_notice(self.conn, task_id, notice, self.clock.now_ms())
            result = self._task(task_id)
        return result

    async def unignore(self, task_id: int) -> sqlite3.Row:
        task = self._task(task_id)
        # 已忽略的视频被替换后，点“开始”按当前文件处理（立项 8.6），所以快照取现在的文件
        ident = await self._snapshot(task)
        target = self._pick(lambda n: (naming.restore_path(task["orig_rel_path"], n),))[0]
        return await self._begin(task, "unignore", ident, target, None)

    async def redo(self, task_id: int) -> sqlite3.Row:
        task = self._task(task_id)
        ident = await self._snapshot(task)
        target = self._pick(lambda n: (naming.restore_path(task["orig_rel_path"], n),))[0]
        old_md = task["md_path"] if await asyncio.to_thread(self.lib.exists, task["md_path"]) else None
        return await self._begin(task, "redo", ident, target, old_md)

    def pick_commit_paths(self, title: str, video_ext: str) -> naming.CommitPaths:
        """选不重名的 md 和视频目标：两处都没被占用才可用，用同一个编号（立项第 6 节）。"""
        def candidates(n: int) -> tuple[str, str]:
            paths = naming.commit_paths(title, n, video_ext)
            return paths.md_path, paths.video_path

        n = self._pick_number(candidates)
        return naming.commit_paths(title, n, video_ext)

    async def run_commit(self, task_id: int) -> sqlite3.Row | None:
        """worker 写好 commit 记录后调用。源视频被替换时任务被删除，返回 None。"""
        return await self._run(task_id)

    async def recover_all(self) -> None:
        """启动时执行没做完的记录。影响所有任务的错误向上抛，剩下的记录留到下次。"""
        for op in repo.list_ops(self.conn):
            try:
                await self._run(op["task_id"])
            except Conflict:
                pass  # 用户操作没能完成，原因已写进操作提示

    # ---- 执行 ----

    async def _begin(
        self, task: sqlite3.Row, kind: OpKind, ident: Ident, target: str, old_md: str | None
    ) -> sqlite3.Row:
        repo.begin_op(self.conn, task["id"], kind, task["current_video_path"], ident, target, old_md,
                      self.clock.now_ms())
        result = await self._run(task["id"])
        if result is None:
            raise NotFound("视频已被删除或移走，任务已删除")
        return result

    async def _run(self, task_id: int) -> sqlite3.Row | None:
        """一步步执行到记录被删除；任务因视频消失或被替换而删除时返回 None。"""
        while (op := repo.get_op(self.conn, task_id)) is not None:
            await self._steps[op["kind"], op["phase"]](op)
        return repo.get_task(self.conn, task_id)

    # commit：recorded → md_placed → video_moved

    async def _commit_recorded(self, op: sqlite3.Row) -> None:
        tmp, target = op["md_tmp_path"], op["md_target_path"]
        tmp_id = await asyncio.to_thread(self.lib.ident, tmp)
        target_id = await asyncio.to_thread(self.lib.ident, target)
        # 目标位置的 md 是不是这次放的：和临时文件同一个 inode，或者临时文件已经移走
        ours = target_id is not None and (tmp_id is None or tmp_id[:2] == target_id[:2])
        if await self._source_replaced(op):
            # 源视频被替换：撤销这次提交（删掉已放好的 md），旧任务消失，新文件留给扫描器（立项 8.6）
            if ours:
                await self._fs(self.lib.remove, target)
            self._remove(op, "源视频在保存结果前被替换，丢弃这次结果")
            return
        if ours and tmp_id is not None:
            await self._fs(self.lib.remove, tmp)  # link 之后、unlink 之前断电
        elif target_id is not None and not ours:
            raise GlobalError(f"目标位置被占用：{target}，请移走该文件后点继续")
        elif tmp_id is not None:
            try:
                await self._fs(self.lib.move, tmp, target)
            except FileExistsError:
                raise GlobalError(f"目标位置被占用：{target}，请移走该文件后点继续") from None
        elif target_id is None:
            raise GlobalError(f"{STORAGE_FAULT}：找不到要保存的 md（{tmp}）", storage=True)
        repo.advance_op(self.conn, op["task_id"], "md_placed", None, self.clock.now_ms())

    async def _commit_md_placed(self, op: sqlite3.Row) -> None:
        try:
            outcome = await self._move_video(op)
        except FileExistsError:
            raise GlobalError(f"目标位置被占用：{op['video_target_path']}，请移走该文件后点继续") from None
        except OSError as exc:
            raise classify_os_error(exc) from exc
        task_id, now = op["task_id"], self.clock.now_ms()
        if outcome == "moved":
            repo.advance_op(self.conn, task_id, "video_moved", op["video_target_path"], now)
        elif outcome == "missing":
            repo.finish_commit(self.conn, task_id, op["title"], op["md_target_path"], None, now)
            self._log(task_id, "warning", "原视频已不存在，md 已保存")
        else:
            await self._fs(self.lib.remove, op["md_target_path"])
            self._remove(op, "源视频在移动前被替换，丢弃这次结果")

    async def _commit_video_moved(self, op: sqlite3.Row) -> None:
        repo.finish_commit(self.conn, op["task_id"], op["title"], op["md_target_path"],
                           op["video_target_path"], self.clock.now_ms())
        self._log(op["task_id"], "info", f"已完成：{op['md_target_path']}")

    # redo：recorded → video_restored → old_md_deleted

    async def _redo_recorded(self, op: sqlite3.Row) -> None:
        if await self._user_move(op, missing="abort"):
            repo.advance_op(self.conn, op["task_id"], "video_restored", op["video_target_path"],
                            self.clock.now_ms())

    async def _redo_video_restored(self, op: sqlite3.Row) -> None:
        if op["old_md_path"] is not None:
            # 删不掉时向上抛“影响所有任务”，记录停在这一步，修好后从这里继续
            await self._fs(self.lib.remove, op["old_md_path"])
        repo.advance_op(self.conn, op["task_id"], "old_md_deleted", None, self.clock.now_ms())

    async def _redo_old_md_deleted(self, op: sqlite3.Row) -> None:
        repo.finish_redo(self.conn, op["task_id"], self.clock.now_ms())
        self._log(op["task_id"], "info", "已移回 input，重新排队")

    # ignore：recorded → video_moved

    async def _ignore_recorded(self, op: sqlite3.Row) -> None:
        if await self._user_move(op, missing="remove"):
            repo.advance_op(self.conn, op["task_id"], "video_moved", op["video_target_path"],
                            self.clock.now_ms())

    async def _ignore_video_moved(self, op: sqlite3.Row) -> None:
        repo.finish_ignore(self.conn, op["task_id"], self.clock.now_ms())
        self._log(op["task_id"], "info", f"已忽略，视频移到 {op['video_target_path']}")

    # unignore：recorded → video_restored

    async def _unignore_recorded(self, op: sqlite3.Row) -> None:
        if await self._user_move(op, missing="remove"):
            repo.advance_op(self.conn, op["task_id"], "video_restored", op["video_target_path"],
                            self.clock.now_ms())

    async def _unignore_video_restored(self, op: sqlite3.Row) -> None:
        repo.finish_unignore(self.conn, op["task_id"], self.clock.now_ms())
        self._log(op["task_id"], "info", "已取消忽略，重新排队")

    # ---- 共用步骤 ----

    async def _user_move(self, op: sqlite3.Row, missing: Literal["abort", "remove"]) -> bool:
        """redo / ignore / unignore 的移动。返回 False 表示操作已放弃（记录已删除）。"""
        try:
            outcome = await self._move_video(op)
        except FileExistsError:
            self._abort(op, NOTICE_TARGET_TAKEN)
        except GlobalError:
            raise
        except OSError as exc:
            self._abort(op, f"剪切失败：{exc.strerror or exc}")
        if outcome == "moved":
            return True
        if outcome == "replaced":
            self._abort(op, NOTICE_CHANGED_RETRY if op["kind"] != "ignore" else NOTICE_CHANGED)
        if missing == "abort":
            self._abort(op, NOTICE_MISSING)
        self._remove(op, "视频已被删除或移走")
        return False

    async def _move_video(self, op: sqlite3.Row) -> Outcome:
        """移动视频。读写错误原样抛出：用户操作写操作提示，commit 按存储问题处理。"""
        src, dst = op["src_path"], op["video_target_path"]
        snap = repo.row_ident(op)
        dst_id = await asyncio.to_thread(self.lib.ident, dst)
        src_id = await asyncio.to_thread(self.lib.ident, src)
        if dst_id == snap:
            if src_id is not None and src_id[:2] == snap[:2]:
                await asyncio.to_thread(self.lib.remove, src)  # link 之后、unlink 之前断电
            return "moved"
        if src_id == snap:
            await asyncio.to_thread(self.lib.move, src, dst)
            return "moved"
        return "missing" if src_id is None else "replaced"

    async def _source_replaced(self, op: sqlite3.Row) -> bool:
        """源视频是否已被替换（不在目标位置，源路径上是另一个文件）。"""
        snap = repo.row_ident(op)
        if await asyncio.to_thread(self.lib.ident, op["video_target_path"]) == snap:
            return False
        src_id = await asyncio.to_thread(self.lib.ident, op["src_path"])
        return src_id is not None and src_id != snap

    async def _fs(self, fn: Callable[..., object], *args: str) -> None:
        """在线程里做文件操作；FileExistsError 原样抛出，其他读写错误按存储问题处理。"""
        try:
            await asyncio.to_thread(fn, *args)
        except (FileExistsError, GlobalError):
            raise
        except OSError as exc:
            raise classify_os_error(exc) from exc

    def _abort(self, op: sqlite3.Row, notice: str) -> NoReturn:
        repo.abort_op(self.conn, op["task_id"], notice, self.clock.now_ms())
        self._log(op["task_id"], "warning", f"操作没有完成：{notice}")
        raise Conflict(notice)

    def _remove(self, op: sqlite3.Row, reason: str) -> None:
        self._log(op["task_id"], "warning", f"任务已删除：{reason}")
        repo.remove_task(self.conn, op["task_id"])

    def _refuse(self, task: sqlite3.Row, notice: str) -> NoReturn:
        repo.set_notice(self.conn, task["id"], notice, self.clock.now_ms())
        raise Conflict(notice)

    def _task(self, task_id: int) -> sqlite3.Row:
        task = repo.get_task(self.conn, task_id)
        if task is None:
            raise NotFound("任务不存在")
        if repo.get_op(self.conn, task_id) is not None:
            raise Conflict("正在执行上一个操作，请稍候")
        return task

    async def _snapshot(self, task: sqlite3.Row) -> Ident:
        path = task["current_video_path"]
        ident = await asyncio.to_thread(self.lib.ident, path) if path else None
        if ident is None:
            self._refuse(task, NOTICE_MISSING)
        return ident  # type: ignore[return-value]

    def _pick(self, candidates: Callable[[int], tuple[str, ...]]) -> tuple[str, ...]:
        return candidates(self._pick_number(candidates))

    def _pick_number(self, candidates: Callable[[int], tuple[str, ...]]) -> int:
        """从不加编号开始逐个试：文件系统里不存在，也没被数据库里的任务或记录占用。"""
        for n in range(naming.MAX_NUMBER + 1):
            paths = candidates(n)
            if not any(self.lib.exists(p) or repo.path_taken(self.conn, p) for p in paths):
                return n
        raise Conflict("同名文件太多，请先整理目标文件夹")

    def _log(self, task_id: int, level: str, message: str) -> None:
        tasklog.write_task(self.conn, self.clock.now_ms(), task_id, level, message)

