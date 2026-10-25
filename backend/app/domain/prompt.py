"""提示词和固定的返回格式（立项附录 A、B）。"""

from collections.abc import Mapping
from typing import Any

FIELDS = ("title", "main_content", "highlight", "transcript")

# 立项附录 A。默认提示词只在这里保存一份，首次启动写库和“恢复默认”都从这里读
DEFAULT_PROMPTS: dict[str, str] = {
    "title": "视频的主题。20 字以内。",
    "main_content": "简述视频主要内容，分段总结视频每段主要内容，视频最终结论。",
    "highlight": "总结视频最大的亮点。",
    "transcript": "逐字转写所有人声内容，按话题分段，加标点。不写画面上的文字，没有人声时写无语音。",
}

SCHEMA_NAME = "vidlex_note"
# 超过 512MB 只传音频时加在提示词最前面（2026-10-24 用户确认）
AUDIO_ONLY_HINT = "这次只有音频，没有画面；标题和各部分内容只根据声音判断。"


def build_prompt(prompts: Mapping[str, str]) -> str:
    """发给 AI 的 4 行：每行开头用和返回字段相同的英文名。"""
    return "\n".join(f"{field}：{prompts[field].strip()}" for field in FIELDS)


def response_schema() -> dict[str, Any]:
    """返回格式由软件固定；严格模式要求所有字段必填、不允许多余字段。"""
    string = {"type": "string"}
    strings = {"type": "array", "items": string}
    return {
        "type": "object",
        "properties": {
            "title": string,
            "main_content": {
                "type": "object",
                "properties": {"overview": string, "sections": strings, "conclusion": string},
                "required": ["overview", "sections", "conclusion"],
                "additionalProperties": False,
            },
            "highlight": string,
            "transcript": strings,
        },
        "required": list(FIELDS),
        "additionalProperties": False,
    }


def text_format() -> dict[str, Any]:
    """Responses API 的 text.format，扁平写法（阶段 0 实测：嵌套一层 json_schema 会被拒绝）。"""
    return {"type": "json_schema", "name": SCHEMA_NAME, "schema": response_schema(), "strict": True}
