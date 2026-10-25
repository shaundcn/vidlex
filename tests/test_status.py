"""对照立项 4.1、4.3 和技术选型 13.5、13.9。"""

import pytest

from app.domain import status
from app.domain.errors import CONSECUTIVE_FAILURES, Conflict

# 立项 4.3 的表格：状态 → 能点的按钮（视频存在、没有进度记录）
TABLE = {
    "queued": ["stop", "ignore"],
    "processing": ["stop"],
    "completed": ["start"],
    "failed": ["start", "ignore"],
    "stopped": ["start", "ignore"],
    "ignored": ["start"],
}


@pytest.mark.parametrize("state, expected", TABLE.items())
def test_allowed_actions_match_table(state: str, expected: list[str]) -> None:
    step = "upload" if state == "processing" else None
    assert status.allowed_actions(state, step, has_op=False, has_video=True) == expected


@pytest.mark.parametrize("step", ["upload", "analyze", "render_md"])
def test_processing_can_stop_before_commit(step: str) -> None:
    assert status.allowed_actions("processing", step, False, True) == ["stop"]


def test_commit_step_cannot_stop() -> None:
    assert status.allowed_actions("processing", "commit", True, True) == []
    with pytest.raises(Conflict, match="正在保存结果，不能停止"):
        status.action_effect("processing", "commit", True, True, "stop")


@pytest.mark.parametrize("state", status.STATUSES)
def test_no_actions_while_op_in_progress(state: str) -> None:
    assert status.allowed_actions(state, "upload" if state == "processing" else None, True, True) == []


def test_completed_without_video_cannot_start() -> None:
    assert status.allowed_actions("completed", None, False, has_video=False) == []
    with pytest.raises(Conflict, match="文件不存在"):
        status.action_effect("completed", None, False, False, "start")


@pytest.mark.parametrize(
    "state, action, effect",
    [("queued", "stop", "stop_queued"), ("queued", "ignore", "ignore"),
     ("processing", "stop", "cancel"), ("completed", "start", "redo"),
     ("failed", "start", "enqueue"), ("failed", "ignore", "ignore"),
     ("stopped", "start", "enqueue"), ("stopped", "ignore", "ignore"),
     ("ignored", "start", "unignore")],
)
def test_action_effects(state: str, action: str, effect: str) -> None:
    step = "analyze" if state == "processing" else None
    assert status.action_effect(state, step, False, True, action) == effect


@pytest.mark.parametrize(
    "state, action",
    [(s, a) for s in status.STATUSES for a in status.ACTIONS if a not in TABLE[s]],
)
def test_disallowed_actions_conflict(state: str, action: str) -> None:
    with pytest.raises(Conflict):
        status.action_effect(state, "analyze" if state == "processing" else None, False, True, action)


# ---- 队列状态（技术选型 13.5 逐行）----

def test_pause_and_resume() -> None:
    assert status.on_user_queue_action("running", "pause") == status.QueueChange("paused")
    assert status.on_user_queue_action("paused", "resume") == status.QueueChange("running")


def test_resume_from_error_resets_failures() -> None:
    assert status.on_user_queue_action("error", "resume") == status.QueueChange("running", reset_failures=True)


def test_wake() -> None:
    assert status.on_user_queue_action("sleeping", "wake") == status.QueueChange("running")
    assert status.wake_if_sleeping("sleeping") == status.QueueChange("running")
    assert status.wake_if_sleeping("paused") is None


@pytest.mark.parametrize(
    "state, action",
    [("paused", "pause"), ("sleeping", "pause"), ("error", "pause"), ("running", "resume"),
     ("sleeping", "resume"), ("running", "wake"), ("paused", "wake"), ("error", "wake")],
)
def test_invalid_queue_actions(state: str, action: str) -> None:
    with pytest.raises(Conflict):
        status.on_user_queue_action(state, action)


@pytest.mark.parametrize("state", ["running", "sleeping"])
def test_global_error_from_running(state: str) -> None:
    assert status.on_global_error(state, False, "网络", True) == status.QueueChange("error", "网络", True)
    assert status.on_global_error(state, False, "Key", False) == status.QueueChange("error", "Key", False)


def test_global_error_while_paused_never_auto_resumes() -> None:
    assert status.on_global_error("paused", False, "网络", True) == status.QueueChange("error", "网络", False)


def test_global_error_while_error_keeps_manual() -> None:
    assert status.on_global_error("error", False, "网络", True).auto_resume is False
    assert status.on_global_error("error", True, "网络", True).auto_resume is True


def test_network_recovered() -> None:
    assert status.on_network_recovered("error", True) == status.QueueChange("running")
    assert status.on_network_recovered("error", False) is None
    assert status.on_network_recovered("running", False) is None


def test_consecutive_failures() -> None:
    assert status.after_task_result(True, 0, "running", False) == (1, None)
    assert status.after_task_result(True, 1, "running", False) == (2, None)
    count, change = status.after_task_result(True, 2, "running", False)
    assert count == 3 and change == status.QueueChange("error", CONSECUTIVE_FAILURES, False)
    assert status.after_task_result(False, 2, "running", False) == (0, None)
    _, change = status.after_task_result(True, 2, "paused", False)
    assert change == status.QueueChange("error", CONSECUTIVE_FAILURES, False)


def test_should_sleep() -> None:
    idle = dict(current="running", sleep_enabled=True, idle_ms=30 * 60_000, idle_minutes=30,
                has_queued=False, has_processing=False, has_pending_files=False)
    assert status.should_sleep(**idle)
    for change in [{"idle_ms": 30 * 60_000 - 1}, {"sleep_enabled": False}, {"has_queued": True},
                   {"has_processing": True}, {"has_pending_files": True},
                   {"current": "paused"}, {"current": "error"}, {"current": "sleeping"}]:
        assert not status.should_sleep(**{**idle, **change}), change
