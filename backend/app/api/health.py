"""健康检查（技术选型 13.11）。"""

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_db
from app.api.schemas import HealthOut

router = APIRouter()


# 路由一律写 async def：同步函数会被放进线程池，而数据库只能在主线程用（技术选型 13.3）
@router.get("/health", response_model=HealthOut, dependencies=[Depends(get_db)])
async def health(request: Request) -> HealthOut:
    state = request.app.state
    return HealthOut(status="ok", schema_version=state.schema_version, ffmpeg=state.ffmpeg)
