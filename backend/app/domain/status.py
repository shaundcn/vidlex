"""任务和队列的状态规则（立项 4.1、4.3，技术选型 13.5、13.9）。"""

from dataclasses import dataclass
from typing import Literal

from app.domain.errors import CONSECUTIVE_FAILURES, Conflict

# 测试用它检查每种状态都定义了按钮规则；步骤和队列状态的取值由数据库的 CHECK 约束
STATUSES = ("queued", "processing", "completed", "failed", "stopped", "ignored")
ACTIONS = ("start", "stop", "ignore")
# 代码里会写入的取值，和数据库 CHECK 约束一致（compress 已不再写入）；写错时 pyright 直接报出
Step = Literal["transcode", "upload", "analyze", "render_md", "commit"]
OpKind = Literal["commit", "redo", "ignore", "unignore"]
FAILURES_TO_ERROR = 3

# 立项 4.3 的按钮表：状态 → {操作: 效果}
_EFFECTS: dict[str, dict[str, str]] = {
    "queued": {"stop": "stop_queued", "ignore": "ignore"},
    "processing": {"stop": "cancel"},
    "completed": {"start": "redo"},
    "failed": {"start": "enqueue", "ignore": "ignore"},
    "stopped": {"start": "enqueue", "ignore": "ignore"},
    "ignored": {"start": "unignore"},
}


def allowed_actions(status: str, step: str | None, has_op: bool, has_video: bool) -> list[str]:
    """当前能点的按钮，按 start、stop、ignore 的顺序。"""
    return [a for a in ACTIONS if _refusal(status, step, has_op, has_video, a) is None]


def action_effect(status: str, step: str | None, has_op: bool, has_video: bool, action: str) -> str:
    """点按钮后要执行的效果：enqueue / redo / unignore / stop_queued / cancel / ignore。

    不允许时抛 Conflict，消息就是给用户看的原因。
    """
    refusal = _refusal(status, step, has_op, has_video, action)
    if refusal is not None:
        raise Conflict(refusal)
    return _EFFECTS[status][action]


def _refusal(status: str, step: str | None, has_op: bool, has_video: bool, action: str) -> str | None:
    # 保存结果阶段一定有 commit 记录，所以先判断它，给出更具体的原因
    if status == "processing" and step == "commit" and action == "stop":
        return "正在保存结果，不能停止"
    if has_op:
        return "正在执行上一个操作，请稍候"
    if action not in _EFFECTS[status]:
        return "当前状态不能执行这个操作"
    if action == "start" and not has_video:
        return "文件不存在"
    return None


@dataclass(frozen=True)
class QueueChange:
    state: str
    error_message: str | None = None
    auto_resume: bool = False
    reset_failures: bool = False


def on_user_queue_action(current: str, action: str) -> QueueChange:
    """暂停、继续、开始（唤醒）。不允许时抛 Conflict。"""
    if action == "pause" and current == "running":
        return QueueChange("paused")
    if action == "resume" and current == "paused":
        return QueueChange("running")
    if action == "resume" and current == "error":
        return QueueChange("running", reset_failures=True)
    if action == "wake" and current == "sleeping":
        return QueueChange("running")
    raise Conflict("队列当前状态不能执行这个操作")


def wake_if_sleeping(current: str) -> QueueChange | None:
    """扫描发现新文件、在任务上点“开始”时调用。"""
    return QueueChange("running") if current == "sleeping" else None


def on_global_error(current: str, current_auto: bool, message: str, auto_resume: bool) -> QueueChange:
    """遇到影响所有任务的错误，进入出错。

    已暂停时不自动继续（尊重用户的暂停）；已经在出错时，只要有一个原因不是网络类，就不自动继续。
    """
    if current == "paused":
        auto_resume = False
    elif current == "error":
        auto_resume = auto_resume and current_auto
    return QueueChange("error", message, auto_resume)


def on_network_recovered(current: str, current_auto: bool) -> QueueChange | None:
    return QueueChange("running") if current == "error" and current_auto else None


def after_task_result(
    failed: bool, failures: int, current: str, current_auto: bool
) -> tuple[int, QueueChange | None]:
    """任务结束后：返回新的连续失败数，以及要不要让队列进入出错。"""
    if not failed:
        return 0, None
    failures += 1
    if failures >= FAILURES_TO_ERROR:
        return failures, on_global_error(current, current_auto, CONSECUTIVE_FAILURES, False)
    return failures, None


def should_sleep(
    current: str,
    sleep_enabled: bool,
    idle_ms: int,
    idle_minutes: int,
    has_queued: bool,
    has_processing: bool,
    has_pending_files: bool,
) -> bool:
    """只从运行中进入：空闲满 X 分钟，没有排队、处理中的任务，也没有等待稳定的文件。"""
    return (
        current == "running"
        and sleep_enabled
        and not has_queued
        and not has_processing
        and not has_pending_files
        and idle_ms >= idle_minutes * 60_000
    )
