"""对照技术选型 11.3（已实测项）和 13.10。"""

import errno

import pytest

from app.domain import errors as e


def test_401_key_invalid() -> None:
    err = e.classify_ark(401, "AuthenticationError", "bad key")
    assert isinstance(err, e.GlobalError) and str(err) == e.KEY_INVALID and not err.auto_resume


def test_model_not_found() -> None:
    err = e.classify_ark(404, "InvalidEndpointOrModel.NotFound", "x")
    assert isinstance(err, e.GlobalError) and str(err) == e.MODEL_UNAVAILABLE


def test_invalid_expire_at_means_clock_wrong() -> None:
    err = e.classify_ark(400, "InvalidParameter", "expire_at must be greater", param="expire_at")
    assert isinstance(err, e.GlobalError) and str(err) == e.CLOCK_WRONG


def test_other_invalid_parameter_fails_task() -> None:
    err = e.classify_ark(400, "InvalidParameter", "bad file_id", param="file_id")
    assert type(err) is e.TaskFailed and "file_id" in str(err)


@pytest.mark.parametrize("header, seconds", [("12", 12), (None, 30), ("0", 30), ("abc", 30),
                                             ("Wed, 21 Oct 2026 07:28:00 GMT", 30)])
def test_429_rate_limited(header: str | None, seconds: int) -> None:
    err = e.classify_ark(429, "RateLimitExceeded", "slow down", retry_after=header)
    assert isinstance(err, e.RateLimited) and err.retry_after == seconds


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_retryable(status: int) -> None:
    assert type(e.classify_ark(status, None, None)) is e.Retryable


def test_unknown_code_retryable_with_raw_text() -> None:
    err = e.classify_ark(403, "SomethingNew", "原始消息")
    assert type(err) is e.Retryable and "SomethingNew" in str(err) and "原始消息" in str(err)


def test_resource_not_found_on_delete() -> None:
    assert e.is_already_deleted(404, "ResourceNotFound")
    assert not e.is_already_deleted(404, "InvalidEndpointOrModel.NotFound")
    # 不在删除时出现的 ResourceNotFound 没有实测，按可重试处理
    assert type(e.classify_ark(404, "ResourceNotFound", "x")) is e.Retryable


def test_network_down_auto_resumes() -> None:
    err = e.network_down()
    assert isinstance(err, e.GlobalError) and err.auto_resume


def test_os_errors() -> None:
    assert str(e.classify_os_error(OSError(errno.ENOSPC, "full"))) == e.NO_SPACE
    assert str(e.classify_os_error(OSError(errno.EDQUOT, "quota"))) == e.NO_SPACE
    assert str(e.classify_os_error(OSError(errno.EXDEV, "cross"))) == e.STORAGE_FAULT
    assert str(e.classify_os_error(OSError(errno.EIO, "io"))) == e.STORAGE_FAULT


def test_retry_schedule() -> None:
    # 首次失败后最多再重试 3 次（共 4 次），等待 1、5、15 分钟
    assert [e.retry_delay(n) for n in range(0, 6)] == [None, 60, 300, 900, None, None]
    assert e.NETWORK_RETRY_DELAYS == (10, 30, 60)
