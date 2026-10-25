"""哪些文件该扫描（立项第 3 节，技术选型 13.6）。"""

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov"})
# NAS 系统目录：群晖的缩略图目录、回收站
SKIPPED_DIRS = frozenset({"@eaDir", "#recycle"})


def is_valid_name(name: str) -> bool:
    """文件名不是合法 UTF-8 时，os.scandir 会用代理字符表示无法解码的字节。"""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def should_descend(dirname: str) -> bool:
    """跳过隐藏目录、NAS 系统目录，以及 Safari 下载中的 .download 包。"""
    return (
        is_valid_name(dirname)
        and not dirname.startswith(".")
        and dirname not in SKIPPED_DIRS
        and not dirname.lower().endswith(".download")
    )


def is_video_file(filename: str) -> bool:
    """只认最后一个扩展名，所以 .part、.crdownload 等下载中的临时文件自然被排除。"""
    if not is_valid_name(filename) or filename.startswith("."):
        return False
    dot = filename.rfind(".")
    return dot > 0 and filename[dot:].lower() in VIDEO_EXTENSIONS
