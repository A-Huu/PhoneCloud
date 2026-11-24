"""运行配置：主目录布局与 config.json 读写。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

MB = 1024 * 1024

DEFAULTS: dict[str, object] = {
    "host": "0.0.0.0",
    "port": 8765,
    "max_upload_mb": 5120,
    "min_free_mb": 1024,
    "session_days": 30,
    "thumb_size": 360,
    "access_log": False,
}

_RANGES = {
    "port": (1, 65535),
    "max_upload_mb": (1, 1024 * 1024),
    "min_free_mb": (0, 1024 * 1024),
    "session_days": (1, 3650),
    "thumb_size": (64, 1024),
}


class ConfigError(Exception):
    """config.json 内容不合法。"""


@dataclass(frozen=True)
class Settings:
    home: Path
    host: str
    port: int
    max_upload_mb: int
    min_free_mb: int
    session_days: int
    thumb_size: int
    access_log: bool

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def accounts_dir(self) -> Path:
        return self.home / "accounts"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "phonecloud.db"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def pid_path(self) -> Path:
        return self.data_dir / "phonecloud.pid"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * MB

    @property
    def min_free_bytes(self) -> int:
        return self.min_free_mb * MB

    @property
    def session_seconds(self) -> int:
        return self.session_days * 86400


def default_home() -> Path:
    """主目录：环境变量 > exe 所在目录（打包后）> 当前目录（源码运行）。"""
    env = os.environ.get("PHONECLOUD_HOME")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path.cwd()


def _validate(values: dict[str, object]) -> None:
    for key, default in DEFAULTS.items():
        value = values[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"config.json 中 {key} 必须是 true 或 false")
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"config.json 中 {key} 必须是整数")
            low, high = _RANGES[key]
            if not low <= value <= high:
                raise ConfigError(f"config.json 中 {key} 必须在 {low}～{high} 之间")
        elif not isinstance(value, str) or not value.strip():
            raise ConfigError(f"config.json 中 {key} 必须是非空字符串")


def load_settings(home: Path | None = None, *, create: bool = True, **overrides: object) -> Settings:
    """读取 config.json；文件不存在时按默认值创建。overrides 中为 None 的项会被忽略。"""
    home = Path(home) if home is not None else default_home()
    home = home.expanduser().resolve()
    config_path = home / "config.json"

    values = dict(DEFAULTS)
    if config_path.exists():
        try:
            # utf-8-sig：兼容记事本保存时带的 BOM
            loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"无法读取 {config_path}：{exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"{config_path} 的内容必须是 JSON 对象")
        values.update({k: v for k, v in loaded.items() if k in DEFAULTS})
    elif create:
        home.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")

    values.update({k: v for k, v in overrides.items() if v is not None and k in DEFAULTS})
    _validate(values)
    return Settings(home=home, **values)  # type: ignore[arg-type]
