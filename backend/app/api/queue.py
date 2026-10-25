"""队列状态与操作（立项 4.1、4.2）。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_actions
from app.api.schemas import Counts, SummaryOut
from app.services.actions import Actions

router = APIRouter(prefix="/api")


def summary(request: Request) -> SummaryOut:
    """只读内存；降级模式下仍返回 200，状态显示为出错（13.11）。"""
    view = request.app.state.memview if request.app.state.db is not None else None
    if view is None:
        return SummaryOut(queue_state="error", error_message=request.app.state.degraded_reason,
                          counts=Counts(queued=0, completed=0, failed=0, stopped=0, ignored=0), list_version=0)
    return SummaryOut(queue_state=view.queue_state, error_message=view.error_message,
                      counts=Counts(**view.counts), list_version=view.list_version)


@router.get("/summary", response_model=SummaryOut)
async def get_summary(request: Request) -> SummaryOut:
    return summary(request)


@router.post("/queue/{action}", response_model=SummaryOut)
async def queue_action(
    action: Literal["pause", "resume", "wake"], request: Request,
    actions: Annotated[Actions, Depends(get_actions)],
) -> SummaryOut:
    if action == "pause":
        actions.pause()
    elif action == "resume":
        await actions.resume()
    else:
        actions.wake()
    return summary(request)
