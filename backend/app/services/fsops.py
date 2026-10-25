"""文件操作：身份快照、不覆盖的移动、原子写入、改属主、视频库标识、路径检查（技术选型 13.8）。

所有路径参数都是相对视频库的 POSIX 路径（技术选型 4.3）。这里的函数都是同步的，
调用方用 asyncio.to_thread 放进线程执行（技术选型 13.3）。
"""

import errno
import logging
import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath

from app.domain.errors import STORAGE_FAULT, GlobalError, classify_os_error
from app.domain.stability import Ident

log = logging.getLogger("vidlex.fsops")

TMP_DIR = ".vidlex/tmp"
LIBRARY_ID_FILE = ".vidlex/library-id"
LAYOUT = ("input", "output", "output/video", "output/ignored", TMP_DIR)
# 文件系统不支持硬链接时的错误码；这时退回“先检查目标不存在，再改名”
_NO_HARDLINK = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK})


class Library:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, rel: str) -> Path:
        """相对路径 → 绝对路径；解析符号链接后必须仍在视频库里。"""
        full = self.root / rel
        if not full.resolve().is_relative_to(self.root.resolve()):
            raise ValueError(f"路径不在视频库里：{rel}")
        return full

    def ident(self, rel: str) -> Ident | None:
        try:
            st = os.stat(self.path(rel), follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return None
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def exists(self, rel: str) -> bool:
        return os.path.lexists(self.path(rel))

    def make_dirs(self, rel_dir: str) -> None:
        """逐级创建目录，新建的目录属主改成和视频库一致。"""
        current = self.root
        for part in PurePosixPath(rel_dir).parts:
            current = current / part
            try:
                os.mkdir(current)
            except FileExistsError:
                continue
            self._chown(current)
            _fsync_dir(current.parent)

    def move(self, src_rel: str, dst_rel: str) -> None:
        """不覆盖的移动：先 link 建新名字，再 unlink 删旧名字。目标已存在时抛 FileExistsError。"""
        src, dst = self.path(src_rel), self.path(dst_rel)
        self.make_dirs(str(PurePosixPath(dst_rel).parent))
        try:
            os.link(src, dst)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise GlobalError(f"{STORAGE_FAULT}：input 和 output 不在同一个文件系统", storage=True) from exc
            if exc.errno not in _NO_HARDLINK:
                raise
            self._rename_if_absent(src, dst)
            return
        _fsync_dir(dst.parent)
        try:
            os.unlink(src)
        except OSError:
            # 删不掉旧名字时撤销新名字，避免同一个视频出现在两处
            os.unlink(dst)
            raise
        _fsync_dir(src.parent)
        self._chown(dst)

    def write_atomic(self, rel: str, text: str) -> None:
        """先写 .part 并落盘，再改成正式名字（会覆盖同名文件，只用于临时目录和视频库标识）。"""
        full = self.path(rel)
        self.make_dirs(str(PurePosixPath(rel).parent))
        part = full.with_name(full.name + ".part")
        with open(part, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, full)
        _fsync_dir(full.parent)
        self._chown(full)

    def remove(self, rel: str) -> bool:
        """删除文件；不存在时返回 False。"""
        full = self.path(rel)
        try:
            os.unlink(full)
        except FileNotFoundError:
            return False
        _fsync_dir(full.parent)
        return True

    def remove_tree(self, rel_dir: str) -> None:
        """删除整个目录（任务的临时目录）；不存在时跳过，删不掉只记警告。"""
        try:
            shutil.rmtree(self.path(rel_dir))
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("删除临时目录失败：%s（%s）", rel_dir, exc)

    def ensure_layout(self) -> None:
        for rel in LAYOUT:
            self.make_dirs(rel)

    def clean_tmp(self, keep: set[str]) -> None:
        """删除临时目录下不在 keep 里的子目录（目录名是任务编号）。删不掉只记警告。"""
        tmp = self.path(TMP_DIR)
        # 先一次读完：边删边遍历时，可能读到 keep 算出之后才开始的任务新建的目录
        with os.scandir(tmp) as it:
            entries = list(it)
        for entry in entries:
            if entry.name in keep:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                else:
                    os.unlink(entry.path)
            except OSError as exc:
                log.warning("删除临时文件失败：%s（%s）", entry.path, exc)

    def check(self, stored_id: str | None) -> str:
        """检查视频库能读写、标识和数据库记录一致；返回应记在数据库里的标识。

        第一次启动时创建标识；数据库里没有记录时沿用文件里的（数据库被删除重建）。
        不通过时抛 GlobalError，调用方不得修改任何任务或文件。
        """
        try:
            if not self.root.is_dir():
                raise GlobalError(f"{STORAGE_FAULT}：找不到视频库文件夹，请检查挂载", storage=True)
            id_path = self.path(LIBRARY_ID_FILE)
            file_id = id_path.read_text(encoding="utf-8").strip() if id_path.exists() else None
            if stored_id is not None and file_id != stored_id:
                raise GlobalError(f"{STORAGE_FAULT}：视频库标识与记录不一致，请检查挂载", storage=True)
            os.listdir(self.root)
            self.make_dirs(".vidlex")
            probe = self.path(f".vidlex/.probe-{uuid.uuid4().hex}")
            probe.write_bytes(b"")
            probe.unlink()
            if file_id is None:
                file_id = uuid.uuid4().hex
                self.write_atomic(LIBRARY_ID_FILE, file_id + "\n")
            return file_id
        except OSError as exc:
            raise classify_os_error(exc) from exc

    def _rename_if_absent(self, src: Path, dst: Path) -> None:
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "目标已存在", str(dst))
        os.rename(src, dst)
        _fsync_dir(dst.parent)
        _fsync_dir(src.parent)
        self._chown(dst)

    def _chown(self, path: Path) -> None:
        """属主改成和视频库一致；失败只记警告（群晖 ACL 下可能失败）。"""
        try:
            st = os.stat(self.root)
            if stat.S_ISLNK(os.lstat(path).st_mode):
                return
            os.chown(path, st.st_uid, st.st_gid)
        except OSError as exc:
            log.warning("修改属主失败：%s（%s）", path, exc)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
