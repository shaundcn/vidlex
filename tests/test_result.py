"""对照立项 8.4。"""

import json

import pytest

from app.domain.errors import Retryable
from app.domain.result import NO_HIGHLIGHT, NO_SPEECH, parse_result


def payload(**overrides: object) -> dict:
    data: dict = {
        "title": "标题",
        "main_content": {"overview": "简述", "sections": ["第一段", "第二段"], "conclusion": "结论"},
        "highlight": "亮点",
        "transcript": ["大家好。", "第二段。"],
    }
    data.update(overrides)
    return data


def test_valid() -> None:
    r = parse_result(json.dumps(payload(), ensure_ascii=False))
    assert (r.title, r.overview, r.sections, r.conclusion) == ("标题", "简述", ("第一段", "第二段"), "结论")
    assert r.highlight == "亮点" and r.transcript == ("大家好。", "第二段。")


@pytest.mark.parametrize("fence", ["```json\n{}\n```", "```\n{}\n```", "  ```JSON\n{}```  "])
def test_code_fence_removed(fence: str) -> None:
    raw = fence.replace("{}", json.dumps(payload(), ensure_ascii=False))
    assert parse_result(raw).title == "标题"


@pytest.mark.parametrize(
    "raw",
    ["不是 JSON", "[1, 2]", '{"title": "x"}',
     json.dumps(payload(highlight=None)),
     json.dumps(payload(transcript="一段")),
     json.dumps(payload(transcript=["a", 1])),
     json.dumps(payload(main_content={"overview": "a", "sections": []})),
     json.dumps(payload(main_content="a"))],
)
def test_invalid_is_retryable(raw: str) -> None:
    with pytest.raises(Retryable):
        parse_result(raw)


def test_empty_main_content_is_retryable() -> None:
    with pytest.raises(Retryable, match="主要内容为空"):
        parse_result(json.dumps(payload(main_content={"overview": " ", "sections": ["", " "], "conclusion": ""})))


def test_partial_main_content_ok() -> None:
    r = parse_result(json.dumps(payload(main_content={"overview": "", "sections": ["只有一段"], "conclusion": ""})))
    assert r.overview == "" and r.sections == ("只有一段",) and r.conclusion == ""


def test_empty_title_kept_for_fallback() -> None:
    assert parse_result(json.dumps(payload(title="  "))).title == ""


def test_empty_highlight() -> None:
    assert parse_result(json.dumps(payload(highlight=" "))).highlight == NO_HIGHLIGHT


@pytest.mark.parametrize("transcript", [[], [""], ["无语音"], ["无语音。"], [" 无语音！ "], ["（无语音）"]])
def test_no_speech(transcript: list[str]) -> None:
    assert parse_result(json.dumps(payload(transcript=transcript), ensure_ascii=False)).transcript == (NO_SPEECH,)


def test_speech_mentioning_no_speech_kept() -> None:
    r = parse_result(json.dumps(payload(transcript=["这一段无语音，下一段有。"]), ensure_ascii=False))
    assert r.transcript == ("这一段无语音，下一段有。",)


def test_lone_surrogate_replaced() -> None:
    r = parse_result(json.dumps(payload(title="a\ud800b")))
    assert r.title == "a�b"
    r.title.encode("utf-8")
