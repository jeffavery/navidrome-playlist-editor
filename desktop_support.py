from __future__ import annotations

import json
import os
from pathlib import Path


APP_FOLDER = "NavidromePlaylistEditor"
KEYRING_SERVICE = "Navidrome Playlist Editor"
KEYRING_USER = "Last.fm API key"


def data_folder() -> Path:
    base = os.getenv("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_FOLDER


def read_settings(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def configure_environment() -> Path:
    folder = data_folder()
    folder.mkdir(parents=True, exist_ok=True)
    settings_path = folder / "desktop-settings.json"
    settings = read_settings(settings_path)
    os.environ["DESKTOP_MODE"] = "1"
    os.environ["DATABASE_PATH"] = str(folder / "library.db")
    os.environ["DESKTOP_SETTINGS_PATH"] = str(settings_path)
    os.environ["MUSIC_ROOT"] = settings.get("music_root", str(folder / "unconfigured-music"))
    os.environ["PLAYLIST_ROOT"] = settings.get("playlist_root", str(folder / "unconfigured-playlists"))
    os.environ["MUSIC_HOST_PATH"] = settings.get("music_root", "Not selected")
    os.environ["PLAYLIST_HOST_PATH"] = settings.get("playlist_root", "Not selected")
    try:
        import keyring
        key = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if key:
            os.environ["LASTFM_API_KEY"] = key
    except Exception:
        pass
    return folder


def save_lastfm_key(value: str) -> bool:
    import keyring
    value = (value or "").strip()
    if value:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, value)
        os.environ["LASTFM_API_KEY"] = value
        return True
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.PasswordDeleteError:
        pass
    os.environ.pop("LASTFM_API_KEY", None)
    return False
