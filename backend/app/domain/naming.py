"""文件名规则：拆分来源、清理标题、标题兜底、重名编号、目标路径（立项第 6 节、4.3）。

路径都是相对视频库的 POSIX 路径；orig_rel_path 相对 input（技术选型 4.3）。
是否被占用由 services 检查：从 n=0 开始逐个试，文件系统和数据库里都没有才可用。
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_TITLE_CHARS = 60
MAX_NAME_BYTES = 255  # NAS 文件名上限
MAX_NUMBER = 9999
# 为“(9999)”和扩展名留出的字节
_SUFFIX_RESERVE = len(f"({MAX_NUMBER}).mov".encode())
# 文件名不能用的字符，以及会让 Obsidian 嵌入失效的字符
_FORBIDDEN = frozenset('/\\:*?"<>|[]#^')
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Source:
    author: str
    platform: str
    title_name: str


@dataclass(frozen=True)
class CommitPaths:
    md_path: str
    video_path: str
    video_name: str  # md 里嵌入的名字，带编号


def original_filename(orig_rel_path: str) -> str:
    return PurePosixPath(orig_rel_path).name


def split_name(filename: str) -> tuple[str, str]:
    """拆成 (主名, 扩展名)，扩展名保留原来的大小写。"""
    path = PurePosixPath(filename)
    return path.stem, path.suffix


def split_source(filename: str) -> Source:
    """按 & 拆分：作者 & 平台 & 标题名。全角＆也算；少于 2 个时整个文件名当标题名。"""
    stem = split_name(filename)[0]
    # 在替换后的字符串里找位置，从原字符串切片，标题名里的全角＆保持原样
    normalized = stem.replace("＆", "&")
    first = normalized.find("&")
    second = normalized.find("&", first + 1) if first >= 0 else -1
    if second < 0:
        return Source("", "", stem.strip())
    return Source(stem[:first].strip(), stem[first + 1 : second].strip(), stem[second + 1 :].strip())


def clean_title(text: str) -> str:
    """不能用的字符和控制字符换成空格，合并连续空白，去掉首尾空格和开头的“.”，过长时截断。"""
    chars = (
        " " if ch in _FORBIDDEN or unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch
        for ch in text
    )
    cleaned = _SPACES.sub(" ", "".join(chars)).lstrip(". ").rstrip(" ")
    cleaned = cleaned[:MAX_TITLE_CHARS].rstrip(" ")
    while len(cleaned.encode()) > MAX_NAME_BYTES - _SUFFIX_RESERVE:
        cleaned = cleaned[:-1].rstrip(" ")
    return cleaned


def fallback_title(ai_title: str, filename: str, task_id: int) -> str:
    """AI 标题 → 原文件名里的标题名 → “未命名视频-任务编号”。"""
    return (
        clean_title(ai_title)
        or clean_title(split_source(filename).title_name)
        or f"未命名视频-{task_id}"
    )


def numbered(stem: str, n: int, ext: str) -> str:
    """n=0 时不加编号；否则追加“(n)”。超过 255 字节时从主名末尾截短。"""
    suffix = f"({n}){ext}" if n else ext
    while len((stem + suffix).encode()) > MAX_NAME_BYTES and stem:
        stem = stem[:-1]
    return stem + suffix


def commit_paths(title: str, n: int, video_ext: str) -> CommitPaths:
    """md 放进 output/，视频放进 output/video/，都不保留原目录结构；两者用同一个编号。"""
    video_name = numbered(title, n, video_ext)
    return CommitPaths(
        md_path=f"output/{numbered(title, n, '.md')}",
        video_path=f"output/video/{video_name}",
        video_name=video_name,
    )


def ignore_path(orig_rel_path: str, n: int) -> str:
    """忽略：保留原文件名，不保留原目录结构。"""
    stem, ext = split_name(original_filename(orig_rel_path))
    return f"output/ignored/{numbered(stem, n, ext)}"


def restore_path(orig_rel_path: str, n: int) -> str:
    """取消忽略、重新处理：恢复原文件名和原目录。"""
    rel = PurePosixPath(orig_rel_path)
    stem, ext = split_name(rel.name)
    return str(PurePosixPath("input") / rel.parent / numbered(stem, n, ext))



def display_name(orig_rel_path: str, title: str | None) -> str:
    """列表显示的名称：已完成时是新标题（只有已完成才有 title），否则是原文件名。"""
    return title or original_filename(orig_rel_path)
