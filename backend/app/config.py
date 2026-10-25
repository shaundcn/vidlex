"""运行配置。容器里路径固定为 /data、/library；环境变量只用于开发和测试（技术选型 13.3）。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    library_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vidlex.db"


def load_config(env: Mapping[str, str] = os.environ) -> Config:
    return Config(
        data_dir=Path(env.get("VIDLEX_DATA_DIR", "/data")),
        library_dir=Path(env.get("VIDLEX_LIBRARY_DIR", "/library")),
    )
