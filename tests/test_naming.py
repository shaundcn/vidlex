"""对照立项第 6 节和 4.3。"""

import pytest

from app.domain import naming


@pytest.mark.parametrize(
    "filename, author, platform, title_name",
    [("设计师小王&抖音&PS抠头发教程.mp4", "设计师小王", "抖音", "PS抠头发教程"),
     ("作者&B站&A&B 对比.mp4", "作者", "B站", "A&B 对比"),            # 标题名里有 &
     ("作者＆抖音＆标题＆后半.MOV", "作者", "抖音", "标题＆后半"),       # 全角＆，标题名保持原样
     (" 作者 & 平台 & 标题 .mp4", "作者", "平台", "标题"),              # 每段去掉首尾空格
     ("只有一个&的视频.mp4", "", "", "只有一个&的视频"),               # 少于 2 个 &
     ("普通视频.mov", "", "", "普通视频"),
     ("&&标题.mp4", "", "", "标题")],
)
def test_split_source(filename: str, author: str, platform: str, title_name: str) -> None:
    assert naming.split_source(filename) == naming.Source(author, platform, title_name)


@pytest.mark.parametrize(
    "raw, cleaned",
    [("3 步用 PS 快速抠出发丝", "3 步用 PS 快速抠出发丝"),
     ('a/b\\c:d*e?f"g<h>i|j', "a b c d e f g h i j"),
     ("[剪映] #教程 ^1", "剪映 教程 1"),
     ("PS: 抠图", "PS 抠图"),                      # 连续空格合并
     ("  ..隐藏标题  ", "隐藏标题"),               # 开头的 . 和首尾空格
     (". . 标题", "标题"),
     ("第一行\n第二行\t三\x7f四 五", "第一行 第二行 三 四 五"),
     ("结论是.", "结论是."),
     ("", ""), ("  ...  ", ""), ("###", "")],
)
def test_clean_title(raw: str, cleaned: str) -> None:
    assert naming.clean_title(raw) == cleaned


def test_clean_title_truncates_to_60_chars() -> None:
    assert naming.clean_title("字" * 100) == "字" * 60
    assert naming.clean_title("a" * 59 + " bcd") == "a" * 59  # 截断后去掉末尾空格


def test_clean_title_respects_byte_limit() -> None:
    title = naming.clean_title("😀" * 70)  # 每个 4 字节
    assert len(title) == 60
    name = naming.numbered(title, naming.MAX_NUMBER, ".mov")
    assert len(name.encode()) <= 255


def test_fallback_title() -> None:
    assert naming.fallback_title(" AI 标题 ", "a&b&原标题.mp4", 7) == "AI 标题"
    assert naming.fallback_title("###", "a&b&原标题.mp4", 7) == "原标题"
    assert naming.fallback_title("", "a&b&[#].mp4", 7) == "未命名视频-7"


def test_numbered() -> None:
    assert naming.numbered("标题", 0, ".md") == "标题.md"
    assert naming.numbered("标题", 2, ".MOV") == "标题(2).MOV"
    long_stem = "字" * 84  # 252 字节
    name = naming.numbered(long_stem, 1, ".mp4")
    assert len(name.encode()) <= 255 and name.endswith("(1).mp4")


def test_commit_paths_share_number() -> None:
    paths = naming.commit_paths("教程/设计师小王&抖音&PS抠头发教程.mp4", "3 步用 PS 快速抠出发丝", 0, ".mp4")
    assert paths == naming.CommitPaths(
        "output/教程/3 步用 PS 快速抠出发丝.md",
        "output/video/3 步用 PS 快速抠出发丝.mp4",
        "3 步用 PS 快速抠出发丝.mp4",
    )
    numbered = naming.commit_paths("根目录视频.MOV", "标题", 1, ".MOV")
    assert numbered.md_path == "output/标题(1).md"
    assert numbered.video_path == "output/video/标题(1).MOV"
    assert numbered.video_name == "标题(1).MOV"


def test_top_level_video_folder_md_goes_to_output_video() -> None:
    assert naming.commit_paths("video/a.mp4", "标题", 0, ".mp4").md_path == "output/video/标题.md"


def test_ignore_and_restore_paths() -> None:
    assert naming.ignore_path("教程/子目录/a&b&c.MOV", 0) == "output/ignored/a&b&c.MOV"
    assert naming.ignore_path("教程/a.mp4", 2) == "output/ignored/a(2).mp4"
    assert naming.restore_path("教程/子目录/a.mp4", 0) == "input/教程/子目录/a.mp4"
    assert naming.restore_path("a.mp4", 1) == "input/a(1).mp4"


def test_display_name() -> None:
    assert naming.display_name("教程/a&b&c.mp4", None) == "a&b&c.mp4"
    assert naming.display_name("教程/a&b&c.mp4", "新标题") == "新标题"
