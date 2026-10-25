"""解析、校验方舟返回的 JSON（立项 8.4、附录 A）。"""

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.domain.errors import Retryable

NO_HIGHLIGHT = "（无）"
NO_SPEECH = "（无语音）"
_FENCE = re.compile(r"^```[A-Za-z]*[ \t]*\n(.*?)\n?```$", re.S)
_SURROGATE = re.compile("[\ud800-\udfff]")


@dataclass(frozen=True)
class AnalysisResult:
    title: str  # 未清理，由 naming.fallback_title 处理
    overview: str
    sections: tuple[str, ...]
    conclusion: str
    highlight: str
    transcript: tuple[str, ...]


def parse_result(raw: str) -> AnalysisResult:
    """不合格（不是 JSON、缺字段、类型不对、主要内容为空）时抛 Retryable。"""
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Retryable("返回内容不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise Retryable("返回内容不是 JSON 对象")
    main = _field(data, "main_content", dict)
    overview = _clean(_field(main, "overview", str))
    sections = _paragraphs(_field(main, "sections", list), "main_content.sections")
    conclusion = _clean(_field(main, "conclusion", str))
    if not overview and not sections and not conclusion:
        raise Retryable("主要内容为空")
    transcript = _paragraphs(_field(data, "transcript", list), "transcript")
    if not transcript or _is_no_speech("".join(transcript)):
        transcript = (NO_SPEECH,)
    return AnalysisResult(
        title=_clean(_field(data, "title", str)),
        overview=overview,
        sections=sections,
        conclusion=conclusion,
        highlight=_clean(_field(data, "highlight", str)) or NO_HIGHLIGHT,
        transcript=transcript,
    )


def _field(obj: dict[str, Any], name: str, kind: type) -> Any:
    value = obj.get(name)
    if not isinstance(value, kind):
        raise Retryable(f"返回内容缺少字段或类型不对：{name}")
    return value


def _paragraphs(items: list[Any], name: str) -> tuple[str, ...]:
    if not all(isinstance(item, str) for item in items):
        raise Retryable(f"返回内容缺少字段或类型不对：{name}")
    return tuple(p for p in (_clean(item) for item in items) if p)


def _clean(text: str) -> str:
    # JSON 里的 \ud800 这类单独的代理字符无法写成 UTF-8 文件，换成替换符
    return _SURROGATE.sub("�", text).strip()


def _is_no_speech(text: str) -> bool:
    """只有“无语音”，可能带句号等标点或空白。"""
    core = "".join(ch for ch in text if unicodedata.category(ch)[0] not in "PZ" and not ch.isspace())
    return core == "无语音"
