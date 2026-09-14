import argparse
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from adaptive_music_player import config, console, db
from adaptive_music_player.events.plays import rebuild_plays
from adaptive_music_player.library.scan import ScanResult, scan_library


def cmd_scan(cfg: config.Config) -> None:
    if not cfg.library_path.is_dir():
        raise SystemExit(f"library folder not found: {cfg.library_path}")
    root = cfg.library_path.resolve()

    with closing(db.connect(cfg.db_path)) as conn:
        try:
            result = _index(conn, cfg.library_path, root)
        except KeyboardInterrupt:
            raise SystemExit(
                "Stopped while indexing. This scan's indexing changes were not saved; "
                "run scan again to retry."
            )
        try:
            _analyze(conn, root)
        except KeyboardInterrupt:
            raise SystemExit(
                "Stopped during analysis. Completed batches are saved; "
                "run scan again to retry the unfinished batch."
            )
    if not result.traversal_complete:
        raise SystemExit("library scan incomplete; missing-file detection was skipped")


def _index(conn: sqlite3.Connection, library_path: Path, root: Path) -> ScanResult:
    started = time.monotonic()
    with console.progress_line(shorten_prefix=f"{root}/") as line:
        def on_progress(done: int, total: int | None, path: Path) -> None:
            if total is None:
                line.show(f"Finding music… {done:,} found")
            else:
                album = " / ".join(path.parent.relative_to(root).parts) or path.name
                line.show(f"Scanning {console.bar(done, total)}  {album}")

        result = scan_library(conn, library_path, on_progress)
    print(f"Scanned {result.found:,} songs in {_duration(time.monotonic() - started)}")
    copies = f" · {result.mp3_copies:,} MP3 copies skipped" if result.mp3_copies else ""
    print(f"  {result.added:,} new · {result.missing:,} missing · {len(result.errors):,} unreadable{copies}")
    return result


def _analyze(conn: sqlite3.Connection, root: Path) -> None:
    from adaptive_music_player.features.extract import extract_features, pending_songs

    pending, _ = pending_songs(conn)
    if not pending:
        return
    legacy_song_ids = {
        row["song_id"] for row in conn.execute(
            "SELECT DISTINCT song_id FROM song_features "
            "WHERE input_signature IS NULL AND status = 'ok' AND data IS NOT NULL")
    }
    legacy_count = sum(song.song["id"] in legacy_song_ids for song in pending)
    if legacy_count:
        print(f"Refreshing older saved analysis for {legacy_count:,} "
              f"song{'s' if legacy_count != 1 else ''} once to enable file-change detection.")
    loading = " Loading sound model…" if any("clap" in song.sources for song in pending) else ""
    print(f"Analyzing {len(pending):,} song{'s' if len(pending) != 1 else ''}.{loading}")
    started = time.monotonic()
    with console.progress_line(shorten_prefix=f"{root}/") as line:
        def on_progress(done: int, total: int, label: str) -> None:
            line.show(f"Analyzing {console.bar(done, total)}  {label}")

        result = extract_features(conn, on_progress=on_progress)
    failed = f" · {len(result.failed):,} failed" if result.failed else ""
    print(f"Analyzed {result.analyzed:,} songs in {_duration(time.monotonic() - started)}{failed}")


def _duration(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def cmd_similar(cfg: config.Config, query: str) -> None:
    import numpy as np

    from adaptive_music_player.model.vectors import load_vectors

    with closing(db.connect(cfg.db_path)) as conn:
        vectors = load_vectors(conn)
        songs = {row["id"]: row for row in conn.execute("SELECT id, title, artist FROM songs")}
    if vectors is None:
        raise SystemExit("No analyzed songs yet; run scan first.")

    labels = [f"{songs[song_id]['artist']} — {songs[song_id]['title']}" for song_id in vectors.song_ids]
    matches = [index for index, label in enumerate(labels) if query.lower() in label.lower()]
    if not matches:
        raise SystemExit(f"No analyzed song matches {query!r}.")
    target = matches[0]
    if len(matches) > 1:
        others = "; ".join(labels[index] for index in matches[1:4])
        print(f"{len(matches)} songs match; showing the first. Others: {others}")

    similarity = vectors.combined @ vectors.combined[target]
    print(f"{labels[target]}  ({vectors.tempo[target]:.0f} bpm)")
    for index in [index for index in np.argsort(-similarity) if index != target][:10]:
        print(f"  {similarity[index]:+.2f}  {labels[index]}  ({vectors.tempo[index]:.0f} bpm)")


def cmd_play(cfg: config.Config) -> None:
    from adaptive_music_player.player.app import PlaybackStopped, Player

    with closing(db.connect(cfg.db_path)) as conn:
        player = Player(conn)
        try:
            player.run()
        except PlaybackStopped as exc:
            raise SystemExit(str(exc))
        except KeyboardInterrupt:
            pass
        finally:
            player.close()


def cmd_plays(cfg: config.Config) -> None:
    with closing(db.connect(cfg.db_path)) as conn:
        plays = rebuild_plays(conn)
    finalized = [p for p in plays if not p["censored"]]
    rate = f"{sum(p['completed'] for p in finalized) / len(finalized):.0%}" if finalized else "n/a"
    print(f"{len(plays)} plays ({len(plays) - len(finalized)} censored) · completion rate {rate} · "
          f"{sum(p['early_skip'] for p in plays)} early skips · "
          f"{sum(p['late_skip'] for p in plays)} late skips · "
          f"{sum(p['replayed'] for p in plays)} replays")


def main() -> None:
    parser = argparse.ArgumentParser(prog="adaptive-music-player")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="index the library and update audio analysis")
    sub.add_parser("play", help="start the terminal player")
    sub.add_parser("plays", help="rebuild plays from listening events and summarize them")
    similar = sub.add_parser("similar", help="list the songs that sound most like a song")
    similar.add_argument("query", help="part of the song's artist or title")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    logging.captureWarnings(True)
    cfg = config.load(args.config)
    if args.command == "similar":
        cmd_similar(cfg, args.query)
    else:
        {"scan": cmd_scan, "play": cmd_play, "plays": cmd_plays}[args.command](cfg)


if __name__ == "__main__":
    main()
