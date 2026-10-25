"""程序日志：输出到 stdout，过滤 API Key，成功的轮询请求不记访问日志（技术选型 13.13）。"""

import logging
import re
import sys

_BEARER = re.compile(r"Bearer\s+(\S+)")
_api_key: str | None = None
_POLL_PATHS = ("/health", "/api/summary")


def set_api_key(key: str | None) -> None:
    """保存或更换 Key 后调用，过滤器之后会把它替换掉。"""
    global _api_key
    _api_key = key or None


def mask(text: str) -> str:
    if _api_key:
        text = text.replace(_api_key, "***" + _api_key[-4:])
    return _BEARER.sub(lambda m: "Bearer ***" + m.group(1)[-4:], text)


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        masked = mask(message)
        if masked != message:
            record.msg, record.args = masked, None
        if record.exc_text:
            record.exc_text = mask(record.exc_text)
        return True


class QuietPollFilter(logging.Filter):
    """uvicorn.access 的 args 为 (客户端, 方法, 路径含查询串, HTTP 版本, 状态码)。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _, _, full_path, _, status = record.args  # type: ignore[misc]
        except (TypeError, ValueError):
            return True
        if int(str(status)) >= 400:
            return True
        path, _, query = str(full_path).partition("?")
        if path in _POLL_PATHS:
            return False
        if path == "/api/logs" and "after_id=" in query:
            return False
        return True


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    handler.addFilter(SecretFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").addFilter(QuietPollFilter())
