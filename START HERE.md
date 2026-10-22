# Navidrome Playlist Editor — Windows Standalone

## Start the app

1. Extract the entire ZIP file to a normal folder.
2. Keep `Navidrome Playlist Editor.exe` and the `_internal` folder together.
3. Double-click `Navidrome Playlist Editor.exe`.

No Python, Docker, Node.js, administrator access, or installer is required. Windows 11 already includes the Microsoft Edge WebView2 component used for the app window.

## First-time setup

1. Click the gear button in the upper-right corner.
2. Choose your music library. Local drives, mapped drives, and UNC shares such as `\\nas\music` are supported.
3. Choose a Playlists folder **inside that music library**, such as `\\nas\music\Playlists`.
4. Optionally paste a Last.fm API key. Windows stores it securely in Credential Manager.
5. Click **Scan music library**.

The first scan reads the music files and may take a while. Later scans compare file modification times and normally process only additions and changes.

## Where the app keeps its own data

The library index and folder settings are stored here:

`%LOCALAPPDATA%\NavidromePlaylistEditor`

Deleted playlists are copied to its `deleted-playlists` subfolder before removal.

## Notes

- The app only puts songs already found in your selected music library into a playlist.
- Playlist files use relative paths so Navidrome can read them from the Playlists folder.
- If Windows asks about network access, the app itself is local-only; music-source lookups still require normal internet access.
