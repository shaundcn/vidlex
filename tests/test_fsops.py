"""文件操作（技术选型 13.8）：不覆盖的移动、硬链接退路、改属主、视频库标识。"""

import errno
import logging
import os
from pathlib import Path

import pytest

from app.domain.errors import GlobalError
from app.services.fsops import LIBRARY_ID_FILE, Library

OWNER = 1234


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    root = tmp_path / "library"
    root.mkdir()
    os.chown(root, OWNER, OWNER)  # 测试以 root 运行
    (root / "input").mkdir()
    (root / "input" / "a.mp4").write_bytes(b"video")
    return Library(root)


def test_move_creates_dirs_and_chowns(lib: Library) -> None:
    before = lib.ident("input/a.mp4")
    lib.move("input/a.mp4", "output/video/新/b.mp4")
    assert not lib.exists("input/a.mp4")
    assert lib.ident("output/video/新/b.mp4") == before  # 身份不变：同一个 inode、修改时间
    for rel in ("output", "output/video", "output/video/新", "output/video/新/b.mp4"):
        st = os.stat(lib.root / rel)
        assert (st.st_uid, st.st_gid) == (OWNER, OWNER), rel


def test_move_never_overwrites(lib: Library) -> None:
    (lib.root / "input" / "b.mp4").write_bytes(b"user-file")
    with pytest.raises(FileExistsError):
        lib.move("input/a.mp4", "input/b.mp4")
    assert (lib.root / "input" / "a.mp4").read_bytes() == b"video"
    assert (lib.root / "input" / "b.mp4").read_bytes() == b"user-file"


def test_cross_filesystem_is_storage_fault(lib: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    def exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", exdev)
    with pytest.raises(GlobalError, match="存储故障"):
        lib.move("input/a.mp4", "output/a.mp4")
    assert lib.exists("input/a.mp4")


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP, errno.EMLINK])
def test_no_hardlink_falls_back_to_rename(lib: Library, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    def unsupported(*args, **kwargs):
        raise OSError(code, "no hard links")

    monkeypatch.setattr(os, "link", unsupported)
    lib.move("input/a.mp4", "output/a.mp4")
    assert lib.exists("output/a.mp4") and not lib.exists("input/a.mp4")
    (lib.root / "input" / "c.mp4").write_bytes(b"c")
    with pytest.raises(FileExistsError):
        lib.move("input/c.mp4", "output/a.mp4")  # 退路也不覆盖
    assert (lib.root / "output" / "a.mp4").read_bytes() == b"video"


def test_unlink_failure_rolls_back_new_name(lib: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.unlink

    def unlink(path, *args, **kwargs):
        if str(path).endswith("input/a.mp4"):
            raise PermissionError(errno.EACCES, "denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(PermissionError):
        lib.move("input/a.mp4", "output/a.mp4")
    assert lib.exists("input/a.mp4") and not lib.exists("output/a.mp4")


def test_chown_failure_only_warns(lib: Library, monkeypatch: pytest.MonkeyPatch,
                                  caplog: pytest.LogCaptureFixture) -> None:
    def denied(*args, **kwargs):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "chown", denied)
    with caplog.at_level(logging.WARNING):
        lib.move("input/a.mp4", "output/a.mp4")
    assert lib.exists("output/a.mp4") and "修改属主失败" in caplog.text


def test_write_atomic_and_remove(lib: Library) -> None:
    lib.write_atomic(".vidlex/tmp/1/note.md", "内容\n")
    path = lib.root / ".vidlex/tmp/1/note.md"
    assert path.read_text() == "内容\n" and not path.with_name("note.md.part").exists()
    assert os.stat(path).st_uid == OWNER
    assert lib.remove(".vidlex/tmp/1/note.md") is True
    assert lib.remove(".vidlex/tmp/1/note.md") is False


def test_path_must_stay_in_library(lib: Library, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        lib.path("../outside.mp4")
    (lib.root / "input" / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        lib.path("input/escape/x.mp4")


def test_clean_tmp_keeps_listed(lib: Library) -> None:
    lib.ensure_layout()
    for name in ("1", "2"):
        lib.write_atomic(f".vidlex/tmp/{name}/x.part", "half")
    lib.clean_tmp({"2"})
    assert sorted(p.name for p in (lib.root / ".vidlex/tmp").iterdir()) == ["2"]


# ---- 视频库标识 ----

def test_library_id_first_start_then_match(lib: Library) -> None:
    first = lib.check(None)
    assert (lib.root / LIBRARY_ID_FILE).read_text().strip() == first
    assert lib.check(first) == first


def test_library_id_adopted_when_db_new(lib: Library) -> None:
    existing = lib.check(None)
    assert lib.check(None) == existing  # 数据库被删除重建：沿用文件里的标识


def test_library_id_mismatch(lib: Library) -> None:
    lib.check(None)
    with pytest.raises(GlobalError, match="标识与记录不一致"):
        lib.check("another-library")


def test_library_id_file_missing_but_db_has_one(lib: Library) -> None:
    with pytest.raises(GlobalError, match="标识与记录不一致"):
        lib.check("recorded-id")
    assert not (lib.root / LIBRARY_ID_FILE).exists()  # 不通过时不写任何东西


def test_library_missing(tmp_path: Path) -> None:
    with pytest.raises(GlobalError, match="找不到视频库"):
        Library(tmp_path / "not-mounted").check(None)
