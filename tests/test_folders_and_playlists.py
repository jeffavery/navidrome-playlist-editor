import sqlite3
import tempfile
import unittest
from pathlib import Path

import app


class FolderAndPlaylistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.music = root / "music"
        self.playlists = root / "playlists"
        self.data = root / "data"
        (self.music / "Rock" / "Artist").mkdir(parents=True)
        (self.music / "Jazz").mkdir()
        (self.playlists / "Mixes").mkdir(parents=True)
        app.MUSIC_ROOT = self.music
        app.PLAYLIST_ROOT = self.playlists
        app.DB_PATH = self.data / "library.db"
        app.DELETED_PLAYLIST_ROOT = self.data / "deleted-playlists"
        app.MUSIC_HOST_PATH = r"G:\Music"
        app.PLAYLIST_HOST_PATH = r"G:\Music\Playlists"
        app.PLAYLIST_LIBRARY_PATH = "Playlists"
        app.DESKTOP_MODE = False
        app.DESKTOP_SETTINGS_PATH = self.data / "desktop-settings.json"
        app.scan_state.update(running=False, scanned=0, indexed=0, total=0, changed=0,
                              removed=0, error=None, started=None, finished=None, phase="idle")
        app.enrichment_state.update(running=False, checked=0, enriched=0, error=None)
        app.init_db()
        app.APP.config.update(TESTING=True)
        self.client = app.APP.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def add_track(self, path="Artist/song.flac"):
        with app.database() as db:
            db.execute(
                "INSERT INTO tracks(path,title,artist,album,year,genre,modified,title_key,artist_key) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (path, "Song", "Artist", "Album", 1985, "rock", 1.0, "song", "artist"),
            )

    def test_folder_browser_stays_inside_configured_mount(self):
        response = self.client.get("/api/folders?kind=music")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["name"] for item in response.json["folders"]], ["Jazz", "Rock"])

        response = self.client.get("/api/folders?kind=music&path=../")
        self.assertEqual(response.status_code, 400)

    def test_changing_library_clears_only_rebuildable_index(self):
        self.add_track()
        with app.database() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('last_scan','yesterday')")

        response = self.client.post("/api/settings/folders", json={
            "music_subpath": "Rock", "playlist_subpath": "Mixes",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["library_changed"])
        with sqlite3.connect(app.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='last_scan'").fetchone())
        self.assertTrue((self.music / "Rock").is_dir())

    def test_playlist_paths_and_recoverable_delete(self):
        self.client.post("/api/settings/folders", json={
            "music_subpath": "Rock", "playlist_subpath": "Mixes",
        })
        self.add_track()

        response = self.client.post("/api/playlists", json={
            "name": "Road Songs", "paths": ["Artist/song.flac"],
        })
        self.assertEqual(response.status_code, 200)
        playlist = self.playlists / "Mixes" / "Road Songs.m3u8"
        self.assertEqual(playlist.read_text(), "#EXTM3U\n../../Rock/Artist/song.flac\n")

        response = self.client.delete("/api/playlists/Road%20Songs.m3u8")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(playlist.exists())
        recoveries = list(app.DELETED_PLAYLIST_ROOT.glob("*--Road Songs.m3u8"))
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0].read_text(), "#EXTM3U\n../../Rock/Artist/song.flac\n")

    def test_desktop_folder_selection_and_relative_playlist_paths(self):
        desktop_playlists = self.music / "Playlists"
        desktop_playlists.mkdir()
        app.DESKTOP_MODE = True

        response = self.client.post("/api/settings/desktop-roots", json={
            "kind": "music", "path": str(self.music),
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["roots_configured"])

        response = self.client.post("/api/settings/desktop-roots", json={
            "kind": "playlists", "path": str(desktop_playlists),
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["roots_configured"])

        self.add_track()
        response = self.client.post("/api/playlists", json={
            "name": "Desktop Mix", "paths": ["Artist/song.flac"],
        })
        self.assertEqual(response.status_code, 200)
        playlist = desktop_playlists / "Desktop Mix.m3u8"
        self.assertEqual(playlist.read_text(), "#EXTM3U\n../Artist/song.flac\n")

    def test_desktop_playlist_folder_must_be_inside_music_library(self):
        app.DESKTOP_MODE = True
        self.client.post("/api/settings/desktop-roots", json={
            "kind": "music", "path": str(self.music),
        })
        response = self.client.post("/api/settings/desktop-roots", json={
            "kind": "playlists", "path": str(self.playlists),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("inside", response.json["error"])


if __name__ == "__main__":
    unittest.main()
