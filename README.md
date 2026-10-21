# Navidrome Playlist Editor

A self-hosted playlist builder for music already in your Navidrome library. It
combines public chart and listener data, matches suggestions to local files, and
writes portable `.m3u8` playlists that Navidrome can import during its next scan.

## Features

- Combine Billboard year-end charts, Last.fm favorites, and ListenBrainz data.
- Select one or more decades and include or exclude normalized genres.
- Control popularity, variety, maximum songs per artist, and unwanted versions.
- Use strict matching or allow tracks whose metadata is incomplete.
- Add up to five local seed songs for similar-listener recommendations.
- Add plain-language preferences such as `upbeat songs for driving`.
- Preview, deselect, name, create, and update playlists from the browser.
- Choose the active library and playlist subfolders from a safe folder browser.
- Delete playlist files with confirmation; recovery copies are kept in app data.
- Rescan the local library incrementally with visible progress.
- Gradually cache MusicBrainz release-year and genre enrichment.

The editor never downloads music. It only includes tracks found in the mounted
library index.

## Quick start

Requirements: Docker with Compose and a music folder already used by Navidrome.
The playlist folder must be located inside that music folder.

```bash
cp .env.example .env
```

Edit `.env` and set `MUSIC_PATH` and `PLAYLIST_PATH` to host paths. The optional
`ND_LASTFM_APIKEY` enables the Last.fm source and seed-song recommendations.
`PLAYLIST_PATH` must be inside `MUSIC_PATH`. If it is not the direct `Playlists`
child, set `PLAYLIST_LIBRARY_PATH` to its path relative to `MUSIC_PATH`.

On Windows Docker hosts, `MUSIC_PATH` and `PLAYLIST_PATH` may be drive-letter
paths or Docker-accessible UNC paths. A web browser cannot grant the container
access to a new drive or share: configure those two mounts in `.env` first, then
use the gear button in the app to choose the root or a subfolder within them.

The supplied Compose file joins an external Docker network named `proxy`. Create
it once if your host does not already have it:

```bash
docker network create proxy
docker compose up -d --build
```

Open `http://localhost:8095`, select **Scan music library**, and wait for the
initial index to complete.

## Navidrome integration

Generated files are written to the configured playlist folder. Because that
folder is inside the music library, Navidrome discovers new or updated `.m3u8`
files during its normal library scan. Trigger a Navidrome quick scan when you
want a playlist to appear immediately.

Playlist entries are relative to the playlist folder, making them portable
inside the same music-library layout. Saving an existing playlist name replaces
that file with the currently selected songs.

## Sources and credentials

- **Billboard:** Wikipedia's public year-end Hot 100 pages; no login required.
- **ListenBrainz:** public site-wide listening statistics; no login required.
- **MusicBrainz:** background metadata enrichment; no login required.
- **Last.fm:** tag favorites and similar-track seeds; API key required.

Only the Last.fm API key is passed into the container. Do not commit `.env`.

## Safety and storage

- The full music library is mounted read-only at `/music`.
- Only the configured playlist folder is mounted read/write at `/playlists`.
- The SQLite index lives in `data/library.db` and stores paths and tags, not audio.
- Deleted playlists are removed from the music share and copied to
  `data/deleted-playlists/` for recovery.
- `.env`, `data/`, backups, caches, and compiled files are excluded from Git and
  Docker build context.
- Original release accuracy depends on library tags and cached MusicBrainz data;
  strict rules may intentionally return fewer songs.

## Update

```bash
git pull --ff-only
docker compose up -d --build
```

Health endpoint: `http://localhost:8095/health`.
