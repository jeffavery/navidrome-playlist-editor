from __future__ import annotations

import sys
from pathlib import Path

from desktop_support import configure_environment, save_lastfm_key


configure_environment()

import webview

import app


class DesktopApi:
    def __init__(self):
        self.window = None

    def choose_folder(self, current=""):
        kwargs = {}
        if current and Path(current).is_dir():
            kwargs["directory"] = current
        result = self.window.create_file_dialog(webview.FileDialog.FOLDER, **kwargs)
        return result[0] if result else None

    def save_lastfm_key(self, key=""):
        try:
            configured = save_lastfm_key(key)
            return {"ok": True, "configured": configured}
        except Exception:
            return {"ok": False, "error": "Windows could not save that API key."}


def main():
    app.init_db()
    api = DesktopApi()
    api.window = webview.create_window(
        "Navidrome Playlist Editor",
        app.APP,
        js_api=api,
        width=1280,
        height=860,
        min_size=(900, 650),
        confirm_close=True,
    )
    webview.start(gui="edgechromium", private_mode=False, debug=False)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        app.init_db()
        with app.APP.test_client() as client:
            if client.get("/").status_code != 200 or client.get("/health").status_code != 200:
                raise SystemExit(1)
        raise SystemExit(0)
    main()
