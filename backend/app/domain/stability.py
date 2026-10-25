"""“连续 3 次扫描不变”的判断（立项第 3 节）。状态只在内存里，重启后重新计数。"""

from collections.abc import Mapping
from dataclasses import dataclass

# 文件身份：(设备号, inode, 大小, 修改时间纳秒)
Ident = tuple[int, int, int, int]
REQUIRED_SCANS = 3


@dataclass(frozen=True)
class Seen:
    ident: Ident
    count: int


def observe(
    previous: Mapping[str, Seen], snapshot: Mapping[str, Ident]
) -> tuple[dict[str, Seen], list[str]]:
    """记录本次扫描。snapshot 只含还没有任务的文件。

    返回新状态和本次刚好满 3 次不变的路径；每个文件只报告一次，消失的文件从状态里去掉。
    """
    state: dict[str, Seen] = {}
    stable: list[str] = []
    for path, ident in snapshot.items():
        old = previous.get(path)
        if old is not None and old.ident == ident:
            count = min(old.count + 1, REQUIRED_SCANS)
        else:
            count = 1
        if count == REQUIRED_SCANS and (old is None or old.count < REQUIRED_SCANS):
            stable.append(path)
        state[path] = Seen(ident, count)
    return state, sorted(stable)


def has_pending(state: Mapping[str, Seen]) -> bool:
    """还有正在等待稳定的文件（影响能否休眠）。"""
    return any(seen.count < REQUIRED_SCANS for seen in state.values())
