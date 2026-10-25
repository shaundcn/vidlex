"""生成 md：信息块、正文、防止打乱结构、转写切段（立项第 6 节、8.4）。"""

import json
import re

from app.domain.result import AnalysisResult

TRANSCRIPT_PARAGRAPH_LIMIT = 1000
CONCLUSION_PREFIX = "结论："

# 会改变 md 结构的行首写法：标题、代码块、HTML 块、嵌入、链接定义（%% 和 $$ 在 protect 里整行处理）
_BLOCK_START = re.compile(r"^(#|```|~~~|<|!\[\[|\[)")
# 只由 - = * _ 组成的行：分隔线，或把上一行变成标题
_RULE_LINE = re.compile(r"^[-=*_][-=*_ \t]*$")
_SENTENCE = re.compile(r"[^。！？!?]*[。！？!?]+[”’\"'」』）)]*|[^。！？!?]+$")


def yaml_quote(value: str) -> str:
    """双引号值：json.dumps 转义后，再转义 YAML 不允许直接出现的字符和 3 种 Unicode 换行。"""
    return "".join(ch if _yaml_safe(ch) else _yaml_escape(ch) for ch in json.dumps(value, ensure_ascii=False))


def protect(text: str) -> str:
    """防止 AI 内容打乱 md 结构：去掉行首空白（避免缩进代码块），给结构性写法加反斜杠。"""
    lines = []
    for line in text.splitlines():
        line = line.lstrip()
        if _BLOCK_START.match(line) or _RULE_LINE.match(line):
            line = "\\" + line
        # Obsidian 的 %% 注释和 $$ 公式在行中间也能开始，未闭合时会吞掉后面的内容
        line = line.replace("%%", "\\%\\%").replace("$$", "\\$\\$")
        lines.append(line)
    return "\n".join(lines).strip()


def split_long_paragraph(paragraph: str, limit: int = TRANSCRIPT_PARAGRAPH_LIMIT) -> list[str]:
    """超过 limit 字时在句末标点处切成几段；单句本身超长时不硬切。"""
    if len(paragraph) <= limit:
        return [paragraph]
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE.findall(paragraph):
        if current and len(current) + len(sentence) > limit:
            chunks.append(current.strip())
            current = ""
        current += sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks


def render_md(
    *,
    title: str,
    platform: str,
    author: str,
    original_file: str,
    video_name: str,
    result: AnalysisResult,
) -> str:
    """title 是清理后、不含重名编号的标题；video_name 是带编号的视频文件名。"""
    front = [
        "---",
        f"title: {yaml_quote(title)}",
        f"platform: {yaml_quote(platform)}",
        f"author: {yaml_quote(author)}",
        "tags:",
        f"original_file: {yaml_quote(original_file)}",
        "---",
    ]
    main = [result.overview, *result.sections]
    if result.conclusion:
        conclusion = result.conclusion.removeprefix(CONCLUSION_PREFIX).lstrip()
        main.append(CONCLUSION_PREFIX + conclusion)
    transcript = [chunk for p in result.transcript for chunk in split_long_paragraph(p)]
    blocks = [
        "\n".join(front),
        f"![[{video_name}]]",
        "## 主要内容\n" + _join(main),
        "## 视频亮点\n" + _join([result.highlight]),
        "## 转写\n" + _join(transcript),
    ]
    return "\n\n".join(blocks) + "\n"


def _join(paragraphs: list[str]) -> str:
    return "\n\n".join(p for p in (protect(x) for x in paragraphs) if p)


def _yaml_safe(ch: str) -> bool:
    # YAML 1.1 的可打印字符，去掉 U+0085、U+2028、U+2029（会被当成换行，值被改写）
    code = ord(ch)
    return (
        code in (0x09, 0x0A, 0x0D)
        or 0x20 <= code <= 0x7E
        or (0xA0 <= code <= 0xD7FF and code not in (0x2028, 0x2029))
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def _yaml_escape(ch: str) -> str:
    code = ord(ch)
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"
