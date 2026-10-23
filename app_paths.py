from __future__ import annotations

import os
import platform
from pathlib import Path
from version import LEGACY_DATA_DIR_NAME


# Historical module alias: this is a directory identifier, not a display name.
APP_NAME = LEGACY_DATA_DIR_NAME


def get_app_data_dir() -> Path:
    """Return the platform-appropriate directory for persistent user data."""

    system = platform.system()

    if system == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")

        if local_app_data:
            return Path(local_app_data) / APP_NAME

        return Path.home() / "AppData" / "Local" / APP_NAME

    if system == "Linux":
        xdg_data_home = os.environ.get("XDG_DATA_HOME")

        if xdg_data_home:
            return Path(xdg_data_home).expanduser() / APP_NAME

        return Path.home() / ".local" / "share" / APP_NAME

    if system == "Darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / APP_NAME
        )

    return Path.home() / f".{APP_NAME}"


DATA_DIR = get_app_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
DATABASE_PATH = DATA_DIR / "library.db"


def ensure_data_dir() -> Path:
    """Create the persistent data directory if needed and return it."""

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if os.name != "nt":
        try:
            os.chmod(DATA_DIR, 0o700)
        except OSError:
            pass

    return DATA_DIR
