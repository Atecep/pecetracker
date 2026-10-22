import json
import os
import tempfile
from pathlib import Path

from app_paths import CONFIG_PATH, ensure_data_dir

DEFAULT_SETTINGS = {"steam_profile": "", "steam_id": "", "steam_api_key": ""}


class SettingsError(ValueError):
    """An existing configuration could not be read; its contents are retained."""


def load_settings():
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected a settings object")
        if any(not isinstance(data.get(key, ""), (str, int)) for key in DEFAULT_SETTINGS):
            raise ValueError("Invalid setting type")
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS)
    except (OSError, UnicodeError, ValueError) as error:
        raise SettingsError("The saved configuration is unreadable. Reconnect your account to replace it; the original file will be backed up.") from error
    result = {key: str(data.get(key, "")).strip() for key in DEFAULT_SETTINGS}
    if not result["steam_profile"] and result["steam_id"]:
        result["steam_profile"] = f"https://steamcommunity.com/profiles/{result['steam_id']}/"
    return result


def _atomic_write(path, content):
    """Create with private permissions and replace only after a complete write."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_settings(steam_profile, steam_id, steam_api_key):
    ensure_data_dir()
    data = dict(zip(DEFAULT_SETTINGS, (steam_profile, steam_id, steam_api_key)))
    payload = json.dumps({key: str(value).strip() for key, value in data.items()}, indent=4).encode("utf-8")
    if CONFIG_PATH.exists():
        _atomic_write(CONFIG_PATH.with_suffix(".json.bak"), CONFIG_PATH.read_bytes())
    _atomic_write(CONFIG_PATH, payload)
