"""对照立项第 6 节、8.4；技术选型第 2 节的 YAML 写法。"""

import yaml
from markdown_it import MarkdownIt

from app.domain.mdrender import protect, render_md, split_long_paragraph, yaml_quote
from app.domain.result import AnalysisResult

RESULT = AnalysisResult(
    title="3 步用 PS 快速抠出发丝",
    overview="本视频演示用 PS 快速抠出发丝。",
    sections=("第一步打开图片。", "第二步选择并遮住。"),
    conclusion="结论：选择并遮住最快。",
    highlight="用“选择并遮住”一键处理发丝边缘",
    transcript=("大家好，今天教大家。", "先打开图层面板。"),
)
NASTY = [
    "#话题 #教程", "a: b", "&开头", "*星号", '引号"和\\反斜杠', "制表\t符", "换行\n第二行",
    "DEL\x7f", "NEL\x85", "行分隔 段分隔 ", "emoji 😀", "- 列表", "[链接]", "{花括号}",
    "!感叹", "%百分", "@at", "`反引号", "|竖线", ">大于", "'单引号'", "~", "null", "true", "123",
    "\x00NUL", "﻿BOM", "￾非字符",
]


def split_doc(md: str) -> tuple[dict, str]:
    assert md.startswith("---\n")
    front, body = md[4:].split("\n---\n", 1)
    return yaml.safe_load(front), body


def render(result: AnalysisResult = RESULT, **meta: str) -> str:
    args = dict(title=result.title, platform="抖音", author="设计师小王",
                original_file="教程/设计师小王&抖音&PS抠头发教程.mp4",
                video_name="3 步用 PS 快速抠出发丝.mp4")
    args.update(meta)
    return render_md(result=result, **args)


def test_template_matches_spec() -> None:
    assert render() == (
        "---\n"
        'title: "3 步用 PS 快速抠出发丝"\n'
        'platform: "抖音"\n'
        'author: "设计师小王"\n'
        "tags:\n"
        'original_file: "教程/设计师小王&抖音&PS抠头发教程.mp4"\n'
        "---\n\n"
        "![[3 步用 PS 快速抠出发丝.mp4]]\n\n"
        "## 主要内容\n"
        "本视频演示用 PS 快速抠出发丝。\n\n"
        "第一步打开图片。\n\n"
        "第二步选择并遮住。\n\n"
        "结论：选择并遮住最快。\n\n"
        "## 视频亮点\n"
        "用“选择并遮住”一键处理发丝边缘\n\n"
        "## 转写\n"
        "大家好，今天教大家。\n\n"
        "先打开图层面板。\n"
    )


def test_yaml_round_trip_every_nasty_value() -> None:
    for value in NASTY:
        loaded = yaml.safe_load(f"v: {yaml_quote(value)}")
        assert loaded == {"v": value}, repr(value)


def test_front_matter_round_trip_in_full_document() -> None:
    meta = {"title": "a#b: &c\x7f\x85", "platform": "", "author": '"引号"',
            "original_file": "目录/#话题&抖音& 标题.mp4"}
    front, _ = split_doc(render(**meta))
    assert front == {**meta, "tags": None}
    assert list(front) == ["title", "platform", "author", "tags", "original_file"]


def test_conclusion_prefix_not_duplicated() -> None:
    _, body = split_doc(render())
    assert "结论：结论：" not in body
    plain = AnalysisResult(**{**RESULT.__dict__, "conclusion": "直接写结论"})
    assert "\n结论：直接写结论\n" in split_doc(render(plain))[1]


def test_empty_main_parts_omitted() -> None:
    sparse = AnalysisResult(**{**RESULT.__dict__, "overview": "", "conclusion": ""})
    _, body = split_doc(render(sparse))
    assert "## 主要内容\n第一步打开图片。\n\n第二步选择并遮住。\n\n## 视频亮点" in body


def block_tokens(body: str) -> list:
    return MarkdownIt("commonmark", {"html": False}).parse(body)


def test_structure_survives_hostile_content() -> None:
    hostile = "\n".join([
        "## 假标题", "# 一级", "```", "未闭合代码块", "~~~", "<pre>", "<!-- 注释", "---", "===",
        "* * *", "___", "-", "    缩进代码", "\t制表缩进", "$$", "公式 $$ 中间", "%% 注释", "中间 %% 注释",
        "![[别的文件.mp4]]", "[定义]: 这里很重要", "> 引用", "- 列表项", "1. 有序",
    ])
    nasty = AnalysisResult(
        title="t", overview=hostile, sections=(hostile,), conclusion=hostile,
        highlight=hostile, transcript=(hostile, hostile),
    )
    md = render(nasty)
    front, body = split_doc(md)
    assert front["title"] == "t"
    tokens = block_tokens(body)
    headings = [tokens[i + 1].content for i, t in enumerate(tokens) if t.type == "heading_open"]
    assert headings == ["主要内容", "视频亮点", "转写"]
    assert all(t.tag == "h2" for t in tokens if t.type == "heading_open")
    forbidden = {"fence", "code_block", "hr", "html_block"}
    assert not [t.type for t in tokens if t.type in forbidden]
    # 只有我们自己的那一行嵌入
    assert body.count("\n![[") == 1
    # Obsidian 的注释和公式标记都被转义
    assert "%%" not in body.replace("\\%\\%", "") and "$$" not in body.replace("\\$\\$", "")
    # 内容本身不丢：链接定义那一行仍然渲染成文字
    html = MarkdownIt("commonmark", {"html": False}).render(body)
    assert "[定义]: 这里很重要" in html and "## 假标题" in html


def test_protect_keeps_harmless_markdown() -> None:
    assert protect("  - 列表\n**加粗**\n| a | b |") == "- 列表\n**加粗**\n| a | b |"


def test_split_long_paragraph() -> None:
    sentence = "这是一句话。" * 10  # 60 字
    paragraph = sentence * 30  # 1800 字
    chunks = split_long_paragraph(paragraph)
    assert "".join(chunks) == paragraph
    assert all(len(c) <= 1000 for c in chunks) and len(chunks) == 2
    assert all(c.endswith("。") for c in chunks)
    assert split_long_paragraph("短段落。") == ["短段落。"]
    assert split_long_paragraph("字" * 1500) == ["字" * 1500]  # 没有句号时不硬切
    mixed = "问句吗？" * 200 + "感叹！" * 200 + "English. " * 10
    assert "".join(split_long_paragraph(mixed)) == mixed.strip()


def test_long_transcript_split_in_md() -> None:
    long = AnalysisResult(**{**RESULT.__dict__, "transcript": ("一句话。" * 400,)})
    _, body = split_doc(render(long))
    paragraphs = body.split("## 转写\n", 1)[1].strip().split("\n\n")
    assert len(paragraphs) == 2 and all(len(p) <= 1000 for p in paragraphs)
