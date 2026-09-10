from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    environment: str
    data_dir: Path

    @property
    def database(self) -> Path:
        return self.data_dir / "sona.sqlite3"


def get_app_paths() -> AppPaths:
    project_root = Path(__file__).resolve().parents[2]
    source_checkout = (project_root / "pyproject.toml").is_file() and not getattr(sys, "frozen", False)
    default_environment = "development" if source_checkout else "production"
    environment = os.environ.get("SONA_ENV", default_environment)
    if environment not in {"development", "test", "production"}:
        raise ValueError("SONA_ENV 必须是 development、test 或 production。")

    override = os.environ.get("SONA_DATA_DIR")
    if override:
        data_dir = Path(override).expanduser().resolve() / environment
    elif environment != "production" and source_checkout:
        data_dir = project_root / ".data" / environment
    else:
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "Sona"
        elif sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Sona"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "sona"
        data_dir = base if environment == "production" else base / environment

    return AppPaths(environment=environment, data_dir=data_dir)
