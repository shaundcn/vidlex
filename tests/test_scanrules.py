"""对照立项第 3 节和技术选型 13.6。"""

import os

import pytest

from app.domain import scanrules

BAD_UTF8 = os.fsdecode(b"\xc4\xe3\xba\xc3.mp4")  # GBK 编码的“你好”


@pytest.mark.parametrize(
    "name, expected",
    [("a.mp4", True), ("IMG_0001.MOV", True), ("a.Mp4", True), ("a&b&c.mov", True),
     ("a.mkv", False), ("a.mp4.part", False), ("a.mp4.crdownload", False), ("a.mp4.xltd", False),
     (".hidden.mp4", False), ("._IMG_0001.MOV", False), (".mp4", False), ("mp4", False),
     (BAD_UTF8, False)],
)
def test_is_video_file(name: str, expected: bool) -> None:
    assert scanrules.is_video_file(name) is expected


@pytest.mark.parametrize(
    "name, expected",
    [("教程", True), ("video", True), ("已忽略", True), (".git", False), (".vidlex", False),
     ("@eaDir", False), ("#recycle", False), ("a.mp4.download", False),
     (os.fsdecode(b"\xc4\xe3"), False)],
)
def test_should_descend(name: str, expected: bool) -> None:
    assert scanrules.should_descend(name) is expected


def test_is_valid_name() -> None:
    assert scanrules.is_valid_name("中文名.mp4")
    assert not scanrules.is_valid_name(BAD_UTF8)
