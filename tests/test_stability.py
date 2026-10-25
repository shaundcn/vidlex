"""对照立项第 3 节“连续 3 次扫描不变”。"""

from app.domain.stability import Seen, has_pending, observe

A = (1, 10, 100, 1000)


def scan(state: dict[str, Seen], snapshot: dict[str, tuple[int, int, int, int]]) -> list[str]:
    new, stable = observe(state, snapshot)
    state.clear()
    state.update(new)
    return stable


def test_stable_after_three_unchanged_scans_and_reported_once() -> None:
    state: dict[str, Seen] = {}
    assert scan(state, {"a.mp4": A}) == []
    assert has_pending(state)
    assert scan(state, {"a.mp4": A}) == []
    assert scan(state, {"a.mp4": A}) == ["a.mp4"]
    assert not has_pending(state)
    assert scan(state, {"a.mp4": A}) == []


def test_any_change_resets_count() -> None:
    for changed in [(1, 10, 200, 1000), (1, 10, 100, 2000), (1, 11, 100, 1000), (2, 10, 100, 1000)]:
        state: dict[str, Seen] = {}
        scan(state, {"a.mp4": A})
        scan(state, {"a.mp4": A})
        assert scan(state, {"a.mp4": changed}) == []
        assert scan(state, {"a.mp4": changed}) == []
        assert scan(state, {"a.mp4": changed}) == ["a.mp4"]


def test_missing_file_is_forgotten() -> None:
    state: dict[str, Seen] = {}
    scan(state, {"a.mp4": A})
    scan(state, {"a.mp4": A})
    scan(state, {})
    assert state == {}
    assert scan(state, {"a.mp4": A}) == []


def test_multiple_files_sorted() -> None:
    state: dict[str, Seen] = {}
    for _ in range(3):
        stable = scan(state, {"b.mp4": A, "a.mp4": (1, 11, 1, 1)})
    assert stable == ["a.mp4", "b.mp4"]
