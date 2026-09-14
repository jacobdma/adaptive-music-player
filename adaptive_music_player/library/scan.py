import logging
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from adaptive_music_player.library.artwork import read_artwork

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".ogg", ".opus"}

ProgressCallback = Callable[[int, int | None, Path], None]


@dataclass
class ScanResult:
    found: int = 0
    added: int = 0
    missing: int = 0
    mp3_copies: int = 0
    errors: list[tuple[Path, str]] = field(default_factory=list)
    traversal_complete: bool = True


def read_tags(path: Path) -> dict:
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError("unsupported audio format")
    tags = audio.tags or {}

    def first(key: str) -> str | None:
        values = tags.get(key)
        return (values[0].strip() or None) if values else None

    try:
        cover_art, cover_mime = read_artwork(path)
    except (OSError, ValueError, mutagen.MutagenError) as exc:
        log.warning("ignoring unreadable artwork in %s: %s", path, exc)
        cover_art, cover_mime = None, None

    return {
        "title": first("title") or path.stem,
        "artist": first("artist") or first("albumartist") or "Unknown Artist",
        "album": first("album") or "Unknown Album",
        "track_number": parse_track_number(first("tracknumber")),
        "duration_ms": int(audio.info.length * 1000),
        "cover_art": cover_art,
        "cover_mime": cover_mime,
    }


def parse_track_number(value: str | None) -> int | None:
    """Accepts '3' or '3/12'."""
    try:
        return int(value.split("/")[0]) if value else None
    except ValueError:
        return None


def scan_library(conn: sqlite3.Connection, root: Path,
                 on_progress: ProgressCallback | None = None) -> ScanResult:
    root = root.expanduser().resolve()
    report = on_progress or (lambda done, total, path: None)
    result = ScanResult()
    known = {row["path"] for row in conn.execute("SELECT path FROM songs")}
    seen: set[str] = set()

    def traversal_error(exc: OSError) -> None:
        path = Path(exc.filename) if exc.filename else root
        log.warning("cannot inspect %s: %s", path, exc)
        result.errors.append((path, str(exc)))
        result.traversal_complete = False

    candidates: list[Path] = []
    copies: set[str] = set()
    for directory, directories, filenames in root.walk(on_error=traversal_error):
        directories.sort()
        originals = {Path(name).stem for name in filenames
                     if Path(name).suffix.lower() in AUDIO_EXTENSIONS - {".mp3"}}
        for name in sorted(filenames):
            path = directory / name
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            if path.suffix.lower() == ".mp3" and path.stem in originals:
                copies.add(str(path))
                continue
            try:
                if not stat.S_ISREG(path.stat().st_mode):
                    continue
            except FileNotFoundError:
                continue
            except OSError as exc:
                seen.add(str(path))
                log.warning("skipping %s: %s", path, exc)
                result.errors.append((path, str(exc)))
                continue
            candidates.append(path)
            report(len(candidates), None, path)

    with conn:
        for done, path in enumerate(candidates, 1):
            report(done, len(candidates), path)
            key = str(path)
            try:
                tags = read_tags(path)
            except FileNotFoundError:
                continue
            except Exception as exc:
                seen.add(key)
                log.warning("skipping %s: %s", path, exc)
                result.errors.append((path, str(exc)))
                continue

            seen.add(key)
            result.found += 1
            result.added += key not in known
            conn.execute(
                """
                INSERT INTO songs (
                    path, title, artist, album, track_number, duration_ms,
                    cover_art, cover_mime, available
                )
                VALUES (
                    :path, :title, :artist, :album, :track_number, :duration_ms,
                    :cover_art, :cover_mime, 1
                )
                ON CONFLICT(path) DO UPDATE SET
                    title = excluded.title, artist = excluded.artist, album = excluded.album,
                    track_number = excluded.track_number, duration_ms = excluded.duration_ms,
                    cover_art = excluded.cover_art, cover_mime = excluded.cover_mime,
                    available = 1
                """,
                {"path": key, **tags},
            )

        result.mp3_copies = len(copies)
        for path in copies & known:
            conn.execute("UPDATE songs SET available = 0 WHERE path = ?", (path,))

        # An incomplete walk cannot establish that an unseen file is missing.
        if result.traversal_complete:
            for path in known - seen - copies:
                cursor = conn.execute(
                    "UPDATE songs SET available = 0 WHERE path = ? AND available = 1", (path,))
                result.missing += cursor.rowcount

    return result
