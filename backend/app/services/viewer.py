"""查看页（立项 4.4、6）：读 md、解析信息块、去掉嵌入视频那一行、渲染成 HTML。

只按任务 id 找文件；渲染关闭原始 HTML，保留 markdown-it 默认的危险链接检查（技术选型 13.12）。
"""

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from markdown_it import MarkdownIt

from app.db import repo
from app.domain import naming
from app.domain.errors import NotFound
from app.services.fsops import Library

_EMBED_LINE = re.compile(r"^[ \t]*!\[\[[^\]\n]+\.(?:mp4|mov)\]\][ \t]*\n?", re.IGNORECASE | re.MULTILINE)
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime"}


@dataclass(frozen=True)
class View:
    id: int
    title: str | None
    original_name: str
    status: str
    video_exists: bool
    md_exists: bool
    front_matter: list[tuple[str, str]]
    md_html: str | None


async def load_view(conn: sqlite3.Connection, library: Library, task_id: int) -> View:
    task = _task(conn, task_id)
    video = await asyncio.to_thread(_existing, library, task["current_video_path"])
    md_text = None
    md_path = await asyncio.to_thread(_existing, library, task["md_path"])
    if md_path is not None:
        md_text = await asyncio.to_thread(md_path.read_text, encoding="utf-8", errors="replace")
    front, html = render(md_text) if md_text is not None else ([], None)
    return View(
        id=task["id"], title=task["title"], original_name=naming.original_filename(task["orig_rel_path"]),
        status=task["status"], video_exists=video is not None, md_exists=md_text is not None,
        front_matter=front, md_html=html,
    )


async def video_file(conn: sqlite3.Connection, library: Library, task_id: int) -> tuple[Path, str]:
    """返回视频的真实路径和类型；不存在时 NotFound。"""
    path = await _file(conn, library, task_id, "current_video_path")
    return path, VIDEO_TYPES.get(path.suffix.lower(), "application/octet-stream")


async def md_file(conn: sqlite3.Connection, library: Library, task_id: int) -> Path:
    """返回 md 的真实路径，供下载；不存在时 NotFound。"""
    return await _file(conn, library, task_id, "md_path")


def render(md_text: str) -> tuple[list[tuple[str, str]], str]:
    """返回 (信息块的键值对，按 md 中的顺序；正文 HTML，已去掉嵌入视频那一行)。"""
    text = md_text.replace("\r\n", "\n")
    front: list[tuple[str, str]] = []
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0 and text[end + 4 : end + 5] in ("", "\n"):
            try:
                data = yaml.safe_load(text[4:end])
            except yaml.YAMLError:
                data = None
            if isinstance(data, dict):
                front = [(str(k), _format(v)) for k, v in data.items()]
            text = text[end + 5 :]
    return front, _MARKDOWN.render(_EMBED_LINE.sub("", text))


def _format(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_format(v) for v in value)
    return str(value)


def _task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    task = repo.get_task(conn, task_id)
    if task is None:
        raise NotFound("任务不存在")
    return task


async def _file(conn: sqlite3.Connection, library: Library, task_id: int, column: str) -> Path:
    path = await asyncio.to_thread(_existing, library, _task(conn, task_id)[column])
    if path is None:
        raise NotFound("文件不存在")
    return path


def _existing(library: Library, rel: str | None) -> Path | None:
    if rel is None:
        return None
    try:
        path = library.path(rel)
    except ValueError:
        return None
    return path if path.is_file() else None
