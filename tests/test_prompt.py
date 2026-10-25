"""对照立项附录 A、B。"""

from app.domain import prompt


def test_build_prompt_default() -> None:
    assert prompt.build_prompt(prompt.DEFAULT_PROMPTS) == (
        "title：视频的主题。20 字以内。\n"
        "main_content：简述视频主要内容，分段总结视频每段主要内容，视频最终结论。\n"
        "highlight：总结视频最大的亮点。\n"
        "transcript：逐字转写所有人声内容，按话题分段，加标点。不写画面上的文字，没有人声时写无语音。"
    )


def test_build_prompt_strips_and_keeps_order() -> None:
    prompts = {"transcript": " t ", "highlight": "h", "title": "a", "main_content": "m"}
    assert prompt.build_prompt(prompts) == "title：a\nmain_content：m\nhighlight：h\ntranscript：t"


def test_text_format_is_flat_strict_schema() -> None:
    fmt = prompt.text_format()
    assert set(fmt) == {"type", "name", "schema", "strict"}
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    schema = fmt["schema"]
    assert schema["required"] == ["title", "main_content", "highlight", "transcript"]
    assert schema["additionalProperties"] is False
    main = schema["properties"]["main_content"]
    assert main["required"] == ["overview", "sections", "conclusion"]
    assert main["additionalProperties"] is False
    assert schema["properties"]["transcript"] == {"type": "array", "items": {"type": "string"}}


def test_text_format_returns_fresh_copy() -> None:
    prompt.text_format()["schema"]["required"].append("x")
    assert "x" not in prompt.text_format()["schema"]["required"]
