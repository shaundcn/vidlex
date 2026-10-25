"""查看页与视频播放（立项 4.4）。只按任务 id 找文件，不接收路径（技术选型 13.12）。"""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.api.deps import get_db, get_library
from app.api.schemas import FrontMatterItem, ViewOut
from app.services import viewer
from app.services.fsops import Library

router = APIRouter(prefix="/api/tasks")


@router.get("/{task_id}/view", response_model=ViewOut)
async def view(
    task_id: int, db: Annotated[sqlite3.Connection, Depends(get_db)],
    library: Annotated[Library, Depends(get_library)],
) -> ViewOut:
    data = await viewer.load_view(db, library, task_id)
    return ViewOut(
        id=data.id, title=data.title, original_name=data.original_name, status=data.status,
        video_exists=data.video_exists, md_exists=data.md_exists, md_html=data.md_html,
        front_matter=[FrontMatterItem(key=k, value=v) for k, v in data.front_matter],
    )


@router.get("/{task_id}/video")
async def video(
    task_id: int, db: Annotated[sqlite3.Connection, Depends(get_db)],
    library: Annotated[Library, Depends(get_library)],
) -> FileResponse:
    """FileResponse 自带 Range 请求，浏览器可以拖动播放。"""
    path, media_type = await viewer.video_file(db, library, task_id)
    return FileResponse(path, media_type=media_type)
