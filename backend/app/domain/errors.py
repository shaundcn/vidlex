"""业务错误与错误分类（立项 8.1、8.3，技术选型 11.3、13.10）。

接口类错误（NotFound、Conflict、Degraded）由 main.py 统一转成 HTTP 状态码；
处理类错误（TaskError 的子类）由 worker 决定重试、失败还是让队列进入出错。
未实测的方舟错误码不写在这里（技术选型 11.3），一律按可重试处理。
"""

import errno


class VidlexError(Exception):
    """所有业务错误的基类；消息是给用户看的中文原因。"""


class NotFound(VidlexError):
    pass


class Conflict(VidlexError):
    pass


class Degraded(VidlexError):
    """降级模式：数据库不可用，只提供页面和摘要。"""


class TaskError(VidlexError):
    """处理一个任务时遇到的错误。"""


class Retryable(TaskError):
    """可重试：等 1、5、15 分钟，共最多 4 次尝试。"""


class RateLimited(TaskError):
    """限流：等待 retry_after 秒；60 分钟时限内不消耗尝试次数。"""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TaskFailed(TaskError):
    """任务失败，不重试。"""


class NetworkError(TaskError):
    """连不上、超时、域名解析失败。同一次尝试内重试，仍不通时转成 network_down()。"""


class GlobalError(TaskError):
    """影响所有任务：队列进入出错。

    auto_resume：网络类，恢复后自动继续；storage：存储故障，出错期间停止扫描。
    """

    def __init__(self, message: str, auto_resume: bool = False, storage: bool = False) -> None:
        super().__init__(message)
        self.auto_resume = auto_resume
        self.storage = storage


KEY_INVALID = "API Key 无效或已失效"
MODEL_UNAVAILABLE = "Seed 2.0 Lite 未开通或已下线"
CLOCK_WRONG = "NAS 系统时间不准，请校准时间"
NETWORK_DOWN = "无法连接方舟，网络恢复后自动继续"
NO_SPACE = "存储空间不足"
NO_API_KEY = "请先在设置页填写 API Key"
STORAGE_FAULT = "存储故障"
FILE_BROKEN = "文件不完整或已损坏，请重新复制"
NOT_VIDEO = "文件不是视频（没有画面）"
TRANSCODE_FAILED = "音频转码失败"
TRANSCODED_OVERSIZED = "音频转码后文件超过 512MB，不处理"
OVERSIZED_TOO_LONG = "超过 512MB 且长于 120 分钟，不处理"
OVERSIZED_NO_AUDIO = "超过 512MB 且没有声音，不处理"
CONSECUTIVE_FAILURES = "连续 3 个任务失败"
OUTPUT_TOO_LONG = "内容过长"

MAX_ATTEMPTS = 4
RETRY_DELAYS = (60, 300, 900)  # 第 1、2、3 次尝试失败后的等待秒数
NETWORK_RETRY_DELAYS = (10, 30, 60)  # 同一次尝试内的断网重试
RATE_LIMIT_DEFAULT = 30
ATTEMPT_TIME_LIMIT = 3600  # 每次尝试的上传、预处理、分析合计限时（秒）


def retry_delay(failed_attempts: int) -> int | None:
    """已经失败了 failed_attempts 次，下一次尝试前等多少秒；None 表示不再重试。"""
    if 1 <= failed_attempts < MAX_ATTEMPTS:
        return RETRY_DELAYS[failed_attempts - 1]
    return None


def network_down() -> GlobalError:
    """同一次尝试内按 NETWORK_RETRY_DELAYS 重试后仍不通时使用。"""
    return GlobalError(NETWORK_DOWN, auto_resume=True)


def classify_ark(
    status: int,
    code: str | None,
    message: str | None,
    param: str | None = None,
    retry_after: str | None = None,
) -> TaskError:
    """把方舟的 HTTP 错误转成处理类错误。只映射已实测的错误码。"""
    if status == 401:
        return GlobalError(KEY_INVALID)
    if status == 404 and code == "InvalidEndpointOrModel.NotFound":
        return GlobalError(MODEL_UNAVAILABLE)
    if status == 400 and code == "InvalidParameter":
        if param == "expire_at":
            return GlobalError(CLOCK_WRONG)
        if (param or "").startswith("audio"):
            # param="audio_url duration"：Seed 2.1 Lite 分析 Files API 文件时，内嵌音频切段后最后一段短于 50ms
            # （技术选型 11.6、11.7；2.0 Lite 实测不出现）
            return TaskFailed(f"音频过短或无法解析：{message}")
        return TaskFailed(f"程序参数错误（{param}）：{message}")
    if status == 429:
        return RateLimited("方舟限流", _parse_retry_after(retry_after))
    if status >= 500:
        return Retryable(f"方舟服务端错误 {status}")
    return Retryable(f"方舟返回 {status} {code}：{message}")


def is_already_deleted(status: int, code: str | None) -> bool:
    """删除云端副本时，“文件不存在”视为已删除。"""
    return status == 404 and code == "ResourceNotFound"


def classify_os_error(exc: OSError) -> GlobalError:
    """视频库读写失败：空间不足单独提示，其他都是存储故障。"""
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return GlobalError(NO_SPACE)
    return GlobalError(STORAGE_FAULT, storage=True)


def _parse_retry_after(value: str | None) -> int:
    # 只认秒数；HTTP 日期格式要读当前时间，domain 不读时间，按默认值处理
    try:
        seconds = int(value) if value is not None else RATE_LIMIT_DEFAULT
    except ValueError:
        return RATE_LIMIT_DEFAULT
    return seconds if seconds > 0 else RATE_LIMIT_DEFAULT
