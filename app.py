from __future__ import annotations

import html
import json
import os
import posixpath
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
from mutagen import File as MutagenFile

APP = Flask(__name__)
DESKTOP_MODE = os.getenv("DESKTOP_MODE", "").strip() == "1"
MUSIC_ROOT = Path(os.getenv("MUSIC_ROOT", "/music"))
PLAYLIST_ROOT = Path(os.getenv("PLAYLIST_ROOT", "/playlists"))
DB_PATH = Path(os.getenv("DATABASE_PATH", "/data/library.db"))
DESKTOP_SETTINGS_PATH = Path(os.getenv("DESKTOP_SETTINGS_PATH", DB_PATH.parent / "desktop-settings.json"))
DELETED_PLAYLIST_ROOT = DB_PATH.parent / "deleted-playlists"
MUSIC_HOST_PATH = os.getenv("MUSIC_HOST_PATH", "Music library")
PLAYLIST_HOST_PATH = os.getenv("PLAYLIST_HOST_PATH", "Playlists folder")
PLAYLIST_LIBRARY_PATH = os.getenv("PLAYLIST_LIBRARY_PATH", "Playlists").strip("/\\") or "Playlists"
AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".aiff", ".ape"}
USER_AGENT = "Navidrome-Playlist-Editor/1.0 (https://github.com/jeffavery/navidrome-playlist-editor)"
GENRE_ALIASES = {
    "rock": ("rock",), "classic rock": ("classic rock",),
    "alternative": ("alternative", "alternative rock"), "new wave": ("new wave",),
    "punk": ("punk", "punk rock"), "grunge": ("grunge",),
    "metal": ("metal", "heavy metal"), "pop": ("pop",),
    "soul": ("soul",), "funk": ("funk",), "disco": ("disco",),
    "country": ("country", "country western", "country & western"),
    "rap / hip-hop": ("rap", "hip hop", "hip-hop", "hiphop"),
    "r&b": ("r&b", "rnb", "rhythm and blues"),
    "electronic": ("electronic", "electronica", "dance", "edm"),
    "blues": ("blues",), "jazz": ("jazz",), "folk": ("folk",),
}
DECADE_WORDS = {
    "sixties": 1960, "seventies": 1970, "eighties": 1980,
    "nineties": 1990, "two thousands": 2000, "twenty tens": 2010,
    "twenty twenties": 2020,
}
VERSION_PATTERNS = {
    "live": ("live", "in concert"),
    "remix": ("remix", "mix edit", "club mix", "dance mix"),
    "karaoke": ("karaoke", "tribute to", "made famous by", "sound alike"),
}
PREFERENCE_TAGS = {
    "upbeat": ("upbeat", "energetic"), "driving": ("driving", "road trip", "road-trip"),
    "chill": ("chill", "relaxing", "mellow"), "party": ("party",),
    "workout": ("workout", "exercise", "gym"), "female vocalists": ("female vocalist", "female vocalists"),
    "guitar rock": ("guitar-driven", "guitar driven"), "romantic": ("romantic", "love songs"),
}
scan_state = {
    "running": False, "scanned": 0, "indexed": 0, "total": 0,
    "changed": 0, "removed": 0, "error": None, "started": None,
    "finished": None, "phase": "idle",
}
scan_lock = threading.Lock()
enrichment_state = {"running": False, "checked": 0, "enriched": 0, "error": None}
enrichment_lock = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db():
    with database() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tracks (
                path TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT NOT NULL,
                album TEXT NOT NULL DEFAULT '', year INTEGER, genre TEXT NOT NULL DEFAULT '',
                modified REAL NOT NULL, title_key TEXT NOT NULL, artist_key TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tracks_title_key ON tracks(title_key);
            CREATE INDEX IF NOT EXISTS tracks_artist_key ON tracks(artist_key);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(tracks)")}
        for name, declaration in (
            ("mbid", "TEXT"), ("mb_year", "INTEGER"),
            ("mb_genres", "TEXT NOT NULL DEFAULT ''"), ("enriched_at", "TEXT")
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE tracks ADD COLUMN {name} {declaration}")


def normalize_subpath(value):
    value = str(value or "").strip().replace("\\", "/").strip("/")
    if not value:
        return ""
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Choose a folder inside the configured location.")
    return "/".join(parts)


def mounted_folder(root, relative=""):
    relative = normalize_subpath(relative)
    base = root.resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("Choose a folder inside the configured location.") from exc
    if not candidate.is_dir():
        raise ValueError("That folder is not available to the app.")
    return candidate


def folder_settings():
    if DESKTOP_MODE:
        return {"music_subpath": "", "playlist_subpath": ""}
    with database() as db:
        settings = dict(db.execute(
            "SELECT key,value FROM settings WHERE key IN ('music_subpath','playlist_subpath')"
        ))
    return {
        "music_subpath": normalize_subpath(settings.get("music_subpath", "")),
        "playlist_subpath": normalize_subpath(settings.get("playlist_subpath", "")),
    }


def active_folders():
    if DESKTOP_MODE and not desktop_roots_configured():
        raise ValueError("Choose your music library and Playlists folder in Settings first.")
    settings = folder_settings()
    return (
        mounted_folder(MUSIC_ROOT, settings["music_subpath"]),
        mounted_folder(PLAYLIST_ROOT, settings["playlist_subpath"]),
        settings,
    )


def display_folder(host_root, relative):
    if not relative:
        return host_root
    separator = "\\" if "\\" in host_root and "/" not in host_root else "/"
    return host_root.rstrip("/\\") + separator + relative.replace("/", separator)


def read_desktop_settings():
    if not DESKTOP_MODE or not DESKTOP_SETTINGS_PATH.is_file():
        return {}
    try:
        payload = json.loads(DESKTOP_SETTINGS_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def desktop_roots_configured():
    if not DESKTOP_MODE:
        return True
    settings = read_desktop_settings()
    music = Path(settings.get("music_root", "")) if settings.get("music_root") else None
    playlists = Path(settings.get("playlist_root", "")) if settings.get("playlist_root") else None
    if not music or not playlists or not music.is_dir() or not playlists.is_dir():
        return False
    try:
        playlists.resolve().relative_to(music.resolve())
    except (OSError, ValueError):
        return False
    return True


def save_desktop_settings(settings):
    DESKTOP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = DESKTOP_SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    temporary.replace(DESKTOP_SETTINGS_PATH)


def desktop_playlist_entry(relative_track):
    track = MUSIC_ROOT.joinpath(*relative_track.split("/"))
    return os.path.relpath(track, PLAYLIST_ROOT).replace("\\", "/")


def norm(value):
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    value = re.sub(r"\b(feat|featuring|ft)\.?\b.*$", "", value, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def first_tag(tags, names):
    if not tags:
        return ""
    for name in names:
        value = tags.get(name)
        if value:
            if isinstance(value, (list, tuple)):
                value = value[0]
            return str(value)
    return ""


def fallback_metadata(path, music_root):
    stem = re.sub(r"^\s*\d+[ ._-]+", "", path.stem).strip()
    artist = path.parent.parent.name if path.parent.parent != music_root else path.parent.name
    title = stem
    if " - " in stem:
        possible_artist, possible_title = stem.split(" - ", 1)
        if possible_artist and possible_title:
            artist, title = possible_artist, possible_title
    return title, artist


def read_metadata(path, music_root):
    title, artist = fallback_metadata(path, music_root)
    album = path.parent.name
    year = None
    genre = ""
    try:
        media = MutagenFile(path, easy=True)
        tags = media.tags if media else None
        title = first_tag(tags, ("title",)) or title
        artist = first_tag(tags, ("artist", "albumartist", "album artist")) or artist
        album = first_tag(tags, ("album",)) or album
        genre = first_tag(tags, ("genre",))
        date = first_tag(tags, ("date", "year", "originaldate"))
        match = re.search(r"(?:19|20)\d{2}", date)
        year = int(match.group()) if match else None
    except Exception:
        pass
    return title.strip(), artist.strip(), album.strip(), year, genre.strip()


def run_scan():
    with scan_lock:
        music_root, playlist_root, _ = active_folders()
        with database() as db:
            previous_count = db.execute("SELECT value FROM settings WHERE key='last_scan_count'").fetchone()
        total_hint = int(previous_count[0]) if previous_count and previous_count[0].isdigit() else 0
        scan_state.update(running=True, scanned=0, indexed=0, total=total_hint, changed=0,
                          removed=0, error=None, started=utc_now(), finished=None, phase="scanning")
        try:
            found = set()
            batch = []
            with database() as db:
                known_modified = {row["path"]: row["modified"] for row in db.execute("SELECT path,modified FROM tracks")}
            for base, dirs, files in os.walk(music_root, onerror=lambda _: None):
                if Path(base) == playlist_root or Path(base).name.casefold() == "playlists":
                    dirs[:] = []
                    continue
                for filename in files:
                    path = Path(base) / filename
                    if path.suffix.lower() not in AUDIO_EXTENSIONS:
                        continue
                    scan_state["scanned"] += 1
                    try:
                        relative = path.relative_to(music_root).as_posix()
                        modified = path.stat().st_mtime
                    except (OSError, ValueError):
                        continue
                    found.add(relative)
                    if known_modified.get(relative) == modified:
                        scan_state["indexed"] += 1
                        continue
                    title, artist, album, year, genre = read_metadata(path, music_root)
                    batch.append((relative, title, artist, album, year, genre, modified, norm(title), norm(artist)))
                    scan_state["changed"] += 1
                    if len(batch) >= 250:
                        save_batch(batch)
                        batch.clear()
                    scan_state["indexed"] += 1
            if batch:
                save_batch(batch)
            removed = known_modified.keys() - found
            with database() as db:
                db.executemany("DELETE FROM tracks WHERE path=?", ((p,) for p in removed))
                db.execute("INSERT OR REPLACE INTO settings VALUES ('last_scan', ?)", (utc_now(),))
                db.execute("INSERT OR REPLACE INTO settings VALUES ('last_scan_count', ?)", (str(len(found)),))
            scan_state.update(total=len(found), removed=len(removed), finished=utc_now(), phase="complete")
        except Exception as exc:
            scan_state["error"] = str(exc)
            scan_state.update(finished=utc_now(), phase="failed")
        finally:
            scan_state["running"] = False
        if not scan_state["error"]:
            start_enrichment()


def save_batch(rows):
    with database() as db:
        db.executemany("""
            INSERT INTO tracks(path,title,artist,album,year,genre,modified,title_key,artist_key)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
            title=excluded.title,artist=excluded.artist,album=excluded.album,year=excluded.year,
            genre=excluded.genre,modified=excluded.modified,title_key=excluded.title_key,artist_key=excluded.artist_key
        """, rows)


def start_enrichment(limit=250):
    if enrichment_state["running"]:
        return
    threading.Thread(target=run_enrichment, args=(limit,), daemon=True).start()


def run_enrichment(limit=250):
    with enrichment_lock:
        enrichment_state.update(running=True, checked=0, enriched=0, error=None)
        try:
            with database() as db:
                tracks = [dict(row) for row in db.execute(
                    "SELECT path,title,artist FROM tracks WHERE enriched_at IS NULL ORDER BY path LIMIT ?", (limit,)
                )]
            for track in tracks:
                params = {
                    "query": f'recording:"{track["title"].replace(chr(34), " ")}" AND artist:"{track["artist"].replace(chr(34), " ")}"',
                    "fmt": "json", "limit": 3,
                }
                try:
                    response = requests.get("https://musicbrainz.org/ws/2/recording", params=params,
                                            headers={"User-Agent": USER_AGENT}, timeout=25)
                    response.raise_for_status()
                    recordings = response.json().get("recordings", [])
                    best = recordings[0] if recordings and int(recordings[0].get("score", 0)) >= 85 else None
                    genres = []
                    if best:
                        genres = [item.get("name", "") for item in best.get("tags", []) if int(item.get("count", 0)) > 0]
                        genres += [item.get("name", "") for item in best.get("genres", [])]
                    release_date = (best or {}).get("first-release-date", "")
                    year_match = re.match(r"(?:19|20)\d{2}", release_date)
                    with database() as db:
                        db.execute("UPDATE tracks SET mbid=?,mb_year=?,mb_genres=?,enriched_at=? WHERE path=?", (
                            best.get("id") if best else None,
                            int(year_match.group()) if year_match else None,
                            ", ".join(dict.fromkeys(g for g in genres if g))[:500], utc_now(), track["path"],
                        ))
                    if best:
                        enrichment_state["enriched"] += 1
                except requests.RequestException:
                    time.sleep(2)
                    continue
                enrichment_state["checked"] += 1
                time.sleep(1.05)
            with database() as db:
                remaining = db.execute("SELECT COUNT(*) FROM tracks WHERE enriched_at IS NULL").fetchone()[0]
                db.execute("INSERT OR REPLACE INTO settings VALUES ('musicbrainz_remaining', ?)", (str(remaining),))
        except Exception as exc:
            enrichment_state["error"] = str(exc)
        finally:
            enrichment_state["running"] = False


def genre_in_text(canonical, text):
    padded = f" {re.sub(r'[^a-z0-9&]+', ' ', (text or '').lower())} "
    return any(f" {re.sub(r'[^a-z0-9&]+', ' ', alias.lower()).strip()} " in padded
               for alias in GENRE_ALIASES.get(canonical, (canonical,)))


def canonical_genres(values):
    answer = []
    for value in values or []:
        lowered = str(value).strip().lower()
        canonical = next((name for name, aliases in GENRE_ALIASES.items()
                          if lowered == name or lowered in aliases), None)
        if canonical and canonical not in answer:
            answer.append(canonical)
    return answer


def infer_query(prompt):
    lowered = prompt.lower()
    years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", lowered)]
    decade = re.search(r"\b(?:the )?((?:19|20)\d)0s\b", lowered)
    if decade and not years:
        start = int(decade.group(1) + "0")
        years = list(range(start, start + 10))
    short_decade = re.search(r"(?<!\d)['’]?(\d{2})s\b", lowered)
    if short_decade and not years:
        short = int(short_decade.group(1))
        if 0 <= short <= 29:
            start = 2000 + short
        elif 30 <= short <= 99:
            start = 1900 + short
        else:
            start = None
        if start:
            years = list(range(start, start + 10))
    if not years:
        for word, start in DECADE_WORDS.items():
            if word in lowered:
                years = list(range(start, start + 10))
                break
    include_genres, exclude_genres = [], []
    for canonical, aliases in GENRE_ALIASES.items():
        positions = [lowered.find(alias) for alias in (canonical, *aliases) if alias in lowered]
        if not positions:
            continue
        position = min(p for p in positions if p >= 0)
        before = lowered[max(0, position - 24):position]
        destination = exclude_genres if re.search(r"(?:no|without|exclude|excluding|avoid)\s+(?:any\s+)?[^,;]{0,14}$", before) else include_genres
        if canonical not in destination:
            destination.append(canonical)
    include_genres = [g for g in include_genres if g not in exclude_genres]
    preference_tags = [tag for tag, phrases in PREFERENCE_TAGS.items() if any(phrase in lowered for phrase in phrases)]
    tag = include_genres[0] if include_genres else (f"{str(years[0])[:3]}0s" if years else (preference_tags[0] if preference_tags else prompt.strip()))
    return {"years": years, "include_genres": include_genres,
            "exclude_genres": exclude_genres, "preference_tags": preference_tags,
            "tag": tag or "rock"}


def lastfm_candidates(tag, wanted):
    key = os.getenv("LASTFM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Listener Favorites needs a Last.fm API key. Configure ND_LASTFM_APIKEY, then recreate the app.")
    results = []
    page = 1
    while len(results) < min(wanted * 5, 1000) and page <= 10:
        response = requests.get("https://ws.audioscrobbler.com/2.0/", params={
            "method": "tag.gettoptracks", "tag": tag, "api_key": key,
            "format": "json", "limit": 100, "page": page,
        }, headers={"User-Agent": USER_AGENT}, timeout=25)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(payload.get("message", "Last.fm request failed"))
        tracks = payload.get("tracks", {}).get("track", [])
        if not tracks:
            break
        genre_hint = next((name for name in GENRE_ALIASES if genre_in_text(name, tag)), None)
        results.extend({"title": item.get("name", ""), "artist": item.get("artist", {}).get("name", ""),
                        "source": "lastfm", "source_label": f"Last.fm · {tag}",
                        "genre_hints": [genre_hint] if genre_hint else []} for item in tracks)
        page += 1
    return results


def lastfm_similar_candidates(seeds, wanted):
    key = os.getenv("LASTFM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Seed-song matching needs the existing Last.fm API key.")
    groups = {}
    per_seed = min(max(wanted * 3, 75), 250)
    for index, seed in enumerate(seeds):
        response = requests.get("https://ws.audioscrobbler.com/2.0/", params={
            "method": "track.getsimilar", "track": seed["title"], "artist": seed["artist"],
            "api_key": key, "format": "json", "limit": per_seed, "autocorrect": 1,
        }, headers={"User-Agent": USER_AGENT}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            continue
        label = f'Similar to {seed["title"]}'
        groups[f"seed-{index + 1}"] = [{
            "title": item.get("name", ""), "artist": item.get("artist", {}).get("name", ""),
            "source": "seed", "source_label": label,
        } for item in payload.get("similartracks", {}).get("track", [])]
    return groups


def billboard_candidates(years):
    if not years:
        years = [datetime.now().year - 1]
    results = []
    for year in years:
        title = f"Billboard Year-End Hot 100 singles of {year}"
        response = requests.get("https://en.wikipedia.org/w/api.php", params={
            "action": "parse", "page": title, "prop": "text", "format": "json", "formatversion": 2,
        }, headers={"User-Agent": USER_AGENT}, timeout=25)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            continue
        soup = BeautifulSoup(payload["parse"]["text"], "html.parser")
        for table in soup.select("table.wikitable"):
            headers = [cell.get_text(" ", strip=True).lower() for cell in table.select("tr th")[:4]]
            if not any("title" in h or "song" in h for h in headers) or not any("artist" in h for h in headers):
                continue
            for row in table.select("tr")[1:]:
                cells = [cell.get_text(" ", strip=True) for cell in row.select("th,td")]
                if len(cells) >= 3:
                    results.append({"title": cells[1].strip('"“”'), "artist": cells[2],
                                    "source": "billboard", "source_label": f"Billboard · {year}",
                                    "year": year})
            break
    if not results:
        raise RuntimeError("No Billboard year-end chart rows were available for that request.")
    return results


def listenbrainz_candidates(wanted):
    response = requests.get("https://api.listenbrainz.org/1/stats/sitewide/recordings", params={
        "count": min(max(wanted * 5, 100), 1000), "range": "all_time",
    }, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    recordings = response.json().get("payload", {}).get("recordings", [])
    if not recordings:
        raise RuntimeError("ListenBrainz did not return site-wide favorites.")
    return [{
        "title": item.get("track_name") or item.get("recording_name", ""),
        "artist": item.get("artist_name", ""), "source": "listenbrainz",
        "source_label": "ListenBrainz · all-time favorites",
    } for item in recordings]


def combine_candidates(groups, popularity="mixed", variety="balanced"):
    combined = {}
    total_requested = sum(len(items) for items in groups.values())
    agreement_bonus = {"familiar": 1.0, "balanced": 0.65, "adventurous": 0.2}.get(variety, 0.65)
    for source, items in groups.items():
        size = max(len(items), 1)
        source_kind = "seed" if source.startswith("seed-") else (
            "lastfm" if source.startswith("lastfm-") else (
                "billboard" if source.startswith("billboard-") else source
            )
        )
        weight = 1.0
        if popularity == "hits":
            weight = 1.4 if source_kind == "billboard" else 0.7
        elif popularity == "enduring":
            weight = 1.3 if source_kind in {"lastfm", "listenbrainz", "seed"} else 0.75
        for rank, candidate in enumerate(items):
            key = (norm(candidate.get("title")), norm(candidate.get("artist")))
            if not all(key):
                continue
            entry = combined.setdefault(key, {
                "title": candidate["title"], "artist": candidate["artist"],
                "sources": [], "source_labels": [], "score": 0.0,
                "years": [], "genre_hints": [],
            })
            if source not in entry["sources"]:
                entry["sources"].append(source)
                entry["source_labels"].append(candidate["source_label"])
                entry["score"] += (1.0 - (rank / size)) * weight
            if candidate.get("year") and candidate["year"] not in entry["years"]:
                entry["years"].append(candidate["year"])
            for genre in candidate.get("genre_hints", []):
                if genre and genre not in entry["genre_hints"]:
                    entry["genre_hints"].append(genre)
    for entry in combined.values():
        entry["score"] += max(0, len(entry["sources"]) - 1) * agreement_bonus
    return sorted(combined.values(), key=lambda item: (-item["score"], item["artist"].casefold(), item["title"].casefold())), total_requested


def matching_genres(text):
    return [name for name in GENRE_ALIASES if genre_in_text(name, text)]


def check_constraints(track, candidate, query):
    relaxed = []
    candidate_years = candidate.get("years", [])
    effective_year = next((year for year in candidate_years if year in query["years"]), None)
    if effective_year is None:
        effective_year = track.get("mb_year") or track.get("year") or (candidate_years[0] if candidate_years else None)
    if query["years"]:
        if effective_year is None:
            if query["strictness"] == "strict":
                return False, [], None, []
            relaxed.append("release year unavailable")
        elif effective_year not in query["years"]:
            return False, [], effective_year, []

    genre_text = f'{track.get("genre", "")} {track.get("mb_genres", "")}'
    track_genres = list(dict.fromkeys(candidate.get("genre_hints", []) + matching_genres(genre_text)))
    if query["exclude_genres"] and any(g in track_genres for g in query["exclude_genres"]):
        return False, track_genres, effective_year, []
    if query["include_genres"] and not any(g in track_genres for g in query["include_genres"]):
        if not track_genres and query["strictness"] == "flexible":
            relaxed.append("genre unavailable")
        else:
            return False, track_genres, effective_year, []

    version_text = f'{track.get("title", "")} {track.get("album", "")}'.lower()
    for version in query["avoid_versions"]:
        if any(re.search(rf"\b{re.escape(phrase)}\b", version_text) for phrase in VERSION_PATTERNS.get(version, ())):
            return False, track_genres, effective_year, []
    return True, track_genres, effective_year, relaxed


def match_library(candidates, limit, query=None):
    with database() as db:
        library = [dict(row) for row in db.execute("SELECT path,title,artist,album,year,genre,mb_year,mb_genres,title_key,artist_key FROM tracks")]
    if not library:
        raise RuntimeError("The library is empty. Run Scan music library first.")
    by_title = {}
    by_title_prefix = {}
    for track in library:
        by_title.setdefault(track["title_key"], []).append(track)
        if track["title_key"]:
            by_title_prefix.setdefault(track["title_key"][:3], []).append(track)
    matches, seen, artist_counts = [], set(), {}
    for candidate in candidates:
        title_key, artist_key = norm(candidate["title"]), norm(candidate["artist"])
        pool = by_title.get(title_key, [])
        best, score = None, 0.0
        for track in pool:
            current = SequenceMatcher(None, artist_key, track["artist_key"]).ratio() if artist_key else 0.75
            if current > score:
                best, score = track, current
        if not best:
            fuzzy_pool = by_title_prefix.get(title_key[:3], []) if title_key else []
            for track in fuzzy_pool:
                length_tolerance = max(3, int(len(title_key) * 0.2))
                if abs(len(track["title_key"]) - len(title_key)) > length_tolerance:
                    continue
                title_score = SequenceMatcher(None, title_key, track["title_key"]).ratio()
                if title_score < 0.9:
                    continue
                artist_score = SequenceMatcher(None, artist_key, track["artist_key"]).ratio() if artist_key else 0.75
                current = title_score * 0.65 + artist_score * 0.35
                if current > score:
                    best, score = track, current
        if best and score >= 0.64 and best["path"] not in seen:
            allowed, matched_genres, effective_year, relaxed = check_constraints(best, candidate, query)
            if not allowed:
                continue
            artist_count_key = best["artist_key"] or norm(best["artist"])
            if query["max_per_artist"] and artist_counts.get(artist_count_key, 0) >= query["max_per_artist"]:
                continue
            seen.add(best["path"])
            artist_counts[artist_count_key] = artist_counts.get(artist_count_key, 0) + 1
            matches.append({**best, "sources": candidate["sources"], "source_labels": candidate["source_labels"],
                            "agreement": len(candidate["sources"]), "requested_title": candidate["title"],
                            "requested_artist": candidate["artist"], "match_year": effective_year,
                            "matched_genres": matched_genres, "relaxed": relaxed})
            if len(matches) >= limit:
                break
    return matches


def safe_name(value):
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", value).strip(" .-")
    return value[:120] or f"Playlist {datetime.now():%Y-%m-%d}"


def build_query(prompt, filters):
    inferred = infer_query(prompt)
    decade_values = filters.get("decades")
    if not isinstance(decade_values, list):
        decade_values = [filters.get("decade", "any")]
    decade_values = list(dict.fromkeys(str(value).lower() for value in decade_values))
    structured_years = []
    decade_starts = []
    for decade_value in decade_values:
        if decade_value in {"", "any"}:
            continue
        match = re.fullmatch(r"((?:19|20)\d)0s?", decade_value)
        if not match:
            raise ValueError("Choose a valid decade.")
        start = int(match.group(1) + "0")
        if start not in decade_starts:
            decade_starts.append(start)
            structured_years.extend(range(start, start + 10))
    structured_years.sort()
    decade_starts.sort()
    if structured_years and inferred["years"] and not set(inferred["years"]).issubset(structured_years):
        raise ValueError("The selected decade and the decade in your written criteria do not agree.")
    years = structured_years or inferred["years"]
    include_genres = canonical_genres(filters.get("include_genres"))
    exclude_genres = canonical_genres(filters.get("exclude_genres"))
    for genre in inferred["include_genres"]:
        if genre not in include_genres:
            include_genres.append(genre)
    for genre in inferred["exclude_genres"]:
        if genre not in exclude_genres:
            exclude_genres.append(genre)
    overlap = set(include_genres) & set(exclude_genres)
    if overlap:
        raise ValueError(f"A genre cannot be both included and excluded: {', '.join(sorted(overlap))}.")
    popularity = filters.get("popularity") if filters.get("popularity") in {"mixed", "hits", "enduring"} else "mixed"
    variety = filters.get("variety") if filters.get("variety") in {"balanced", "familiar", "adventurous"} else "balanced"
    strictness = "flexible" if filters.get("strictness") == "flexible" else "strict"
    try:
        max_per_artist = int(filters.get("max_per_artist", 2))
    except (TypeError, ValueError):
        max_per_artist = 2
    max_per_artist = max(0, min(max_per_artist, 50))
    avoid_versions = [v for v in filters.get("avoid_versions", []) if v in VERSION_PATTERNS]
    tag_decades = decade_starts or (sorted({year // 10 * 10 for year in years}) if years else [])
    tags = list(dict.fromkeys(include_genres + [f"{start}s" for start in tag_decades] + inferred["preference_tags"]))
    if not tags:
        tags = [inferred["tag"] or "rock"]
    return {
        "years": years, "include_genres": include_genres, "exclude_genres": exclude_genres,
        "tag": tags[0], "tags": tags[:8], "decades": decade_starts,
        "popularity": popularity, "variety": variety,
        "strictness": strictness, "max_per_artist": max_per_artist,
        "avoid_versions": avoid_versions,
    }


def query_summary(query, seed_count=0):
    parts = []
    if query.get("decades"):
        parts.append(" + ".join(f"{start}s" for start in query["decades"]))
    elif query["years"]:
        parts.append(f'{min(query["years"])}–{max(query["years"])}')
    else:
        parts.append("Any decade")
    if query["include_genres"]:
        parts.append("Include " + ", ".join(query["include_genres"]))
    if query["exclude_genres"]:
        parts.append("No " + ", ".join(query["exclude_genres"]))
    parts.append({"mixed": "Big Hits and Still Popular Today", "hits": "Big Hits When Released",
                  "enduring": "Still Popular Today"}[query["popularity"]])
    if query["max_per_artist"]:
        parts.append(f'Maximum {query["max_per_artist"]} per artist')
    if query["avoid_versions"]:
        parts.append("Avoid " + ", ".join(query["avoid_versions"]))
    if seed_count:
        parts.append(f'Similar to {seed_count} seed song' + ("s" if seed_count != 1 else ""))
    parts.append("Strict metadata" if query["strictness"] == "strict" else "Allow unknown metadata")
    return parts


@APP.get("/")
def index():
    return render_template("index.html")


@APP.get("/health")
def health():
    return jsonify(status="ok")


@APP.get("/api/status")
def status():
    with database() as db:
        count = db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        settings = dict(db.execute("SELECT key,value FROM settings"))
    playlists = []
    try:
        _, playlist_root, folder_config = active_folders()
        playlists = sorted((p.name for p in playlist_root.glob("*.m3u8") if p.is_file()), key=str.casefold)
    except (OSError, ValueError):
        folder_config = folder_settings()
    desktop_settings = read_desktop_settings()
    roots_configured = desktop_roots_configured()
    music_display = desktop_settings.get("music_root", "Not selected") if DESKTOP_MODE else display_folder(MUSIC_HOST_PATH, folder_config["music_subpath"])
    playlist_display = desktop_settings.get("playlist_root", "Not selected") if DESKTOP_MODE else display_folder(PLAYLIST_HOST_PATH, folder_config["playlist_subpath"])
    return jsonify(track_count=count, last_scan=settings.get("last_scan"),
                   last_scan_count=settings.get("last_scan_count"), scan=scan_state,
                   enrichment=enrichment_state, musicbrainz_remaining=settings.get("musicbrainz_remaining"),
                   playlists=playlists, lastfm_configured=bool(os.getenv("LASTFM_API_KEY", "").strip()),
                   desktop_mode=DESKTOP_MODE, roots_configured=roots_configured,
                   folders={
                       **folder_config,
                       "music_display": music_display,
                       "playlist_display": playlist_display,
                   })


@APP.get("/api/folders")
def browse_folders():
    if DESKTOP_MODE:
        return jsonify(error="Use the Windows folder picker in the standalone app."), 400
    kind = request.args.get("kind", "")
    if kind not in {"music", "playlists"}:
        return jsonify(error="Choose either the music or playlists location."), 400
    root = MUSIC_ROOT if kind == "music" else PLAYLIST_ROOT
    host_root = MUSIC_HOST_PATH if kind == "music" else PLAYLIST_HOST_PATH
    try:
        relative = normalize_subpath(request.args.get("path", ""))
        current = mounted_folder(root, relative)
        folders = []
        for child in sorted(current.iterdir(), key=lambda item: item.name.casefold()):
            if child.name.startswith("."):
                continue
            try:
                resolved = child.resolve()
                resolved.relative_to(root.resolve())
            except (OSError, ValueError):
                continue
            if resolved.is_dir():
                child_relative = resolved.relative_to(root.resolve()).as_posix()
                folders.append({"name": child.name, "path": child_relative})
        parent = posixpath.dirname(relative) if relative else None
        return jsonify(kind=kind, path=relative, parent=parent, folders=folders[:1000],
                       display=display_folder(host_root, relative), truncated=len(folders) > 1000)
    except (OSError, ValueError) as exc:
        return jsonify(error=str(exc)), 400


@APP.post("/api/settings/folders")
def save_folder_settings():
    if DESKTOP_MODE:
        return jsonify(error="Use the Windows folder picker in the standalone app."), 400
    if scan_state["running"] or enrichment_state["running"]:
        return jsonify(error="Wait for the current library task to finish before changing folders."), 409
    payload = request.get_json(force=True) or {}
    try:
        music_subpath = normalize_subpath(payload.get("music_subpath", ""))
        playlist_subpath = normalize_subpath(payload.get("playlist_subpath", ""))
        mounted_folder(MUSIC_ROOT, music_subpath)
        playlist_folder = mounted_folder(PLAYLIST_ROOT, playlist_subpath)
    except (OSError, ValueError) as exc:
        return jsonify(error=str(exc)), 400
    if not os.access(playlist_folder, os.W_OK):
        return jsonify(error="The selected playlists folder is not writable."), 400

    current = folder_settings()
    library_changed = music_subpath != current["music_subpath"]
    with database() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('music_subpath',?)", (music_subpath,))
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('playlist_subpath',?)", (playlist_subpath,))
        if library_changed:
            db.execute("DELETE FROM tracks")
            db.execute("DELETE FROM settings WHERE key IN ('last_scan','last_scan_count','musicbrainz_remaining')")
    if library_changed:
        scan_state.update(running=False, scanned=0, indexed=0, total=0, changed=0, removed=0,
                          error=None, started=None, finished=None, phase="idle")
    return jsonify(
        message="Folder settings saved. Scan the music library to build the new index."
        if library_changed else "Folder settings saved.",
        library_changed=library_changed,
        folders={
            "music_subpath": music_subpath,
            "playlist_subpath": playlist_subpath,
            "music_display": display_folder(MUSIC_HOST_PATH, music_subpath),
            "playlist_display": display_folder(PLAYLIST_HOST_PATH, playlist_subpath),
        },
    )


@APP.post("/api/settings/desktop-roots")
def save_desktop_roots():
    global MUSIC_ROOT, PLAYLIST_ROOT, MUSIC_HOST_PATH, PLAYLIST_HOST_PATH
    if not DESKTOP_MODE:
        return jsonify(error="This setting is only available in the Windows standalone app."), 404
    if scan_state["running"] or enrichment_state["running"]:
        return jsonify(error="Wait for the current library task to finish before changing folders."), 409
    payload = request.get_json(force=True) or {}
    kind = payload.get("kind")
    if kind not in {"music", "playlists"}:
        return jsonify(error="Choose either the music or playlists location."), 400
    raw_path = str(payload.get("path", "")).strip()
    chosen = Path(raw_path)
    if not raw_path or not chosen.is_absolute() or not chosen.is_dir():
        return jsonify(error="Choose an existing folder."), 400
    chosen = chosen.resolve()
    settings = read_desktop_settings()
    library_changed = False
    if kind == "music":
        old_music = settings.get("music_root", "")
        settings["music_root"] = str(chosen)
        library_changed = os.path.normcase(old_music) != os.path.normcase(str(chosen))
        current_playlists = settings.get("playlist_root")
        if current_playlists:
            try:
                Path(current_playlists).resolve().relative_to(chosen)
            except (OSError, ValueError):
                settings.pop("playlist_root", None)
    else:
        current_music = settings.get("music_root")
        if not current_music or not Path(current_music).is_dir():
            return jsonify(error="Choose the music library first."), 400
        try:
            chosen.relative_to(Path(current_music).resolve())
        except ValueError:
            return jsonify(error="Choose a Playlists folder inside the selected music library."), 400
        if not os.access(chosen, os.W_OK):
            return jsonify(error="The selected Playlists folder is not writable."), 400
        settings["playlist_root"] = str(chosen)
    try:
        save_desktop_settings(settings)
    except OSError as exc:
        return jsonify(error=f"Could not save the folder setting: {exc}"), 500
    MUSIC_ROOT = Path(settings.get("music_root", DB_PATH.parent / "unconfigured-music"))
    PLAYLIST_ROOT = Path(settings.get("playlist_root", DB_PATH.parent / "unconfigured-playlists"))
    MUSIC_HOST_PATH = settings.get("music_root", "Not selected")
    PLAYLIST_HOST_PATH = settings.get("playlist_root", "Not selected")
    if library_changed:
        with database() as db:
            db.execute("DELETE FROM tracks")
            db.execute("DELETE FROM settings WHERE key IN ('last_scan','last_scan_count','musicbrainz_remaining')")
        scan_state.update(running=False, scanned=0, indexed=0, total=0, changed=0, removed=0,
                          error=None, started=None, finished=None, phase="idle")
    return jsonify(message="Folder selected.", library_changed=library_changed,
                   roots_configured=desktop_roots_configured(),
                   folders={"music_subpath": "", "playlist_subpath": "",
                            "music_display": settings.get("music_root", "Not selected"),
                            "playlist_display": settings.get("playlist_root", "Not selected")})


@APP.get("/api/tracks/search")
def search_tracks():
    term = request.args.get("q", "").strip()[:100]
    if len(term) < 2:
        return jsonify(tracks=[])
    like = f"%{term}%"
    with database() as db:
        rows = [dict(row) for row in db.execute("""
            SELECT path,title,artist,album,year,mb_year,genre,mb_genres
            FROM tracks WHERE title LIKE ? COLLATE NOCASE OR artist LIKE ? COLLATE NOCASE
            ORDER BY CASE WHEN title LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,
                     artist COLLATE NOCASE,title COLLATE NOCASE LIMIT 20
        """, (like, like, f"{term}%"))]
    return jsonify(tracks=[{
        "path": row["path"], "title": row["title"], "artist": row["artist"],
        "album": row["album"], "year": row["mb_year"] or row["year"],
        "genres": matching_genres(f'{row["genre"]} {row["mb_genres"]}'),
    } for row in rows])


@APP.post("/api/scan")
def scan():
    if DESKTOP_MODE and not desktop_roots_configured():
        return jsonify(error="Choose your music library and Playlists folder in Settings first."), 400
    if scan_state["running"]:
        return jsonify(scan=scan_state), 202
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify(message="Library scan started"), 202


@APP.post("/api/preview")
def preview():
    payload = request.get_json(force=True)
    prompt = str(payload.get("prompt", "")).strip()
    sources = payload.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    sources = [source for source in sources if source in {"billboard", "lastfm", "listenbrainz"}]
    limit = max(1, min(int(payload.get("limit", 50)), 500))
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    seed_paths = list(dict.fromkeys(p for p in (payload.get("seed_paths") or []) if isinstance(p, str)))[:5]
    include_seeds = bool(payload.get("include_seeds"))
    if not sources and not seed_paths:
        return jsonify(error="Choose at least one music source or add a seed song."), 400
    try:
        query = build_query(prompt, filters)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    seeds = []
    if seed_paths:
        placeholders = ",".join("?" for _ in seed_paths)
        with database() as db:
            found = {row["path"]: dict(row) for row in db.execute(
                f"SELECT path,title,artist,album,year,genre,mb_year,mb_genres FROM tracks WHERE path IN ({placeholders})",
                seed_paths,
            )}
        seeds = [found[path] for path in seed_paths if path in found]
    groups = {}
    errors = []
    if "billboard" in sources:
        if query["years"]:
            try:
                billboard = billboard_candidates(query["years"])
                for year in query["years"]:
                    yearly = [candidate for candidate in billboard if candidate.get("year") == year]
                    if yearly:
                        groups[f"billboard-{year}"] = yearly
            except Exception as exc:
                errors.append(str(exc))
        else:
            errors.append("Billboard was skipped because no decade was selected.")
    if "lastfm" in sources:
        per_tag = max(25, (limit + len(query["tags"]) - 1) // len(query["tags"]))
        for index, tag in enumerate(query["tags"]):
            try:
                groups[f"lastfm-{index + 1}"] = lastfm_candidates(tag, per_tag)
            except Exception as exc:
                errors.append(str(exc))
    if "listenbrainz" in sources:
        try:
            groups["listenbrainz"] = listenbrainz_candidates(limit)
        except Exception as exc:
            errors.append(str(exc))
    if seeds:
        try:
            groups.update(lastfm_similar_candidates(seeds, limit))
        except Exception as exc:
            errors.append(str(exc))
        if include_seeds:
            groups["selected-seeds"] = [{
                "title": seed["title"], "artist": seed["artist"], "source": "seed",
                "source_label": "Selected seed", "year": seed.get("mb_year") or seed.get("year"),
                "genre_hints": matching_genres(f'{seed.get("genre", "")} {seed.get("mb_genres", "")}'),
            } for seed in seeds]
    if not groups:
        return jsonify(error=" ".join(errors) or "No source results were found."), 502
    candidates, requested = combine_candidates(groups, query["popularity"], query["variety"])
    matches = match_library(candidates, limit, query)
    return jsonify(matches=matches, requested=requested, unique_suggestions=len(candidates), query=query,
                   interpretation=query_summary(query, len(seeds)), warnings=errors,
                   relaxed_matches=sum(bool(match["relaxed"]) for match in matches))


@APP.post("/api/playlists")
def create_playlist():
    payload = request.get_json(force=True)
    name = safe_name(str(payload.get("name", "")))
    paths = payload.get("paths") or []
    if not paths:
        return jsonify(error="Choose at least one matched song."), 400
    with database() as db:
        valid = {row[0] for row in db.execute("SELECT path FROM tracks")}
    selected = [p for p in paths if isinstance(p, str) and p in valid]
    if not selected:
        return jsonify(error="None of the selected songs are in the current library index."), 400
    _, playlist_root, folder_config = active_folders()
    target = playlist_root / f"{name}.m3u8"
    existed = target.exists()
    temporary = playlist_root / f".{name}.{os.getpid()}.tmp"
    try:
        playlist_root.mkdir(parents=True, exist_ok=True)
        if DESKTOP_MODE:
            entries = (desktop_playlist_entry(path) for path in selected)
        else:
            logical_playlist_folder = posixpath.join(PLAYLIST_LIBRARY_PATH, folder_config["playlist_subpath"])
            entries = (
                posixpath.relpath(posixpath.join(folder_config["music_subpath"], path), logical_playlist_folder)
                for path in selected
            )
        content = "#EXTM3U\n" + "".join(f"{entry}\n" for entry in entries)
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        return jsonify(error=f"Could not write to the Playlists folder: {exc}"), 500
    action = "Updated" if existed else "Created"
    return jsonify(message=f"{action} {target.name}", filename=target.name,
                   songs=len(selected), action=action.lower())


@APP.delete("/api/playlists/<path:filename>")
def delete_playlist(filename):
    filename = unquote(filename)
    if Path(filename).name != filename or not filename.lower().endswith(".m3u8"):
        return jsonify(error="Choose a valid playlist file."), 400
    try:
        _, playlist_root, _ = active_folders()
        target = playlist_root / filename
        if not target.is_file():
            return jsonify(error="That playlist no longer exists."), 404
        DELETED_PLAYLIST_ROOT.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        recovery = DELETED_PLAYLIST_ROOT / f"{stamp}--{filename}"
        shutil.copy2(target, recovery)
        target.unlink()
    except OSError as exc:
        return jsonify(error=f"Could not delete the playlist: {exc}"), 500
    return jsonify(message=f"Deleted {filename}", filename=filename,
                   recovery_copy=recovery.name)


if __name__ == "__main__":
    init_db()
    APP.run(host="0.0.0.0", port=8000, threaded=True)

