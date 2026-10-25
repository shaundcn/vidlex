"""设置（立项第 5 节）。API Key 只返回开头和末尾各 4 位，中间用 * 代替（技术选型第 5 节）。"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_actions, get_memview, get_runtime, get_selftest
from app.api.schemas import ApiKeyIn, ApiKeyOut, FolderStatus, Prompts, SelfTestOut, SettingsOut, Sleep
from app.services.actions import Actions
from app.services.memview import MemView
from app.services.runtime import Runtime
from app.services.selftest import SelfTest

router = APIRouter(prefix="/api/settings")


@router.get("", response_model=SettingsOut)
async def get_settings(
    view: Annotated[MemView, Depends(get_memview)], runtime: Annotated[Runtime, Depends(get_runtime)],
    actions: Annotated[Actions, Depends(get_actions)],
) -> SettingsOut:
    s = view.settings
    key = s["api_key"]
    library_reason = view.error_message if runtime.storage_fault else None
    return SettingsOut(
        has_api_key=key is not None, api_key_masked=mask_key(key) if key else None,
        prompts=Prompts(title=s["prompt_title"], main_content=s["prompt_main_content"],
                        highlight=s["prompt_highlight"], transcript=s["prompt_transcript"]),
        default_prompts=Prompts(**actions.default_prompts()),
        sleep=Sleep(enabled=bool(s["sleep_enabled"]), idle_minutes=s["sleep_idle_minutes"],
                    interval_hours=s["sleep_interval_hours"]),
        # 只读显示：input、output、ignored 都在视频库里，看是否有存储故障；data 能打开数据库就可读写
        # （降级模式下整个接口 503）
        folders=[*(FolderStatus(name=name, ok=not runtime.storage_fault, reason=library_reason)
                   for name in ("input", "output", "ignored")),
                 FolderStatus(name="data", ok=True, reason=None)],
    )


@router.put("/api-key", response_model=ApiKeyOut)
async def put_api_key(body: ApiKeyIn, actions: Annotated[Actions, Depends(get_actions)]) -> ApiKeyOut:
    actions.save_api_key(body.key)
    return ApiKeyOut(saved=True, masked=mask_key(body.key))


def mask_key(key: str) -> str:
    """开头 4 位 + 中间每个字符一个 * + 末尾 4 位；不超过 8 位时全部用 * 代替。"""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


@router.put("/prompts", response_model=Prompts)
async def put_prompts(body: Prompts, actions: Annotated[Actions, Depends(get_actions)]) -> Prompts:
    actions.save_prompts(body.model_dump())
    return body


@router.put("/sleep", response_model=Sleep)
async def put_sleep(body: Sleep, actions: Annotated[Actions, Depends(get_actions)]) -> Sleep:
    actions.save_sleep(body.enabled, body.idle_minutes, body.interval_hours)
    return body


@router.post("/test", response_model=SelfTestOut)
async def run_selftest(selftest: Annotated[SelfTest, Depends(get_selftest)]) -> SelfTestOut:
    """用当前 Key 处理内置短视频；已有测试在跑时 409。"""
    ok, message = await selftest.run()
    return SelfTestOut(ok=ok, message=message)
