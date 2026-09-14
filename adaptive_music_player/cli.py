import argparse
import logging
from contextlib import closing
from pathlib import Path

from adaptive_music_player import config, db
from adaptive_music_player.events.plays import rebuild_plays
from adaptive_music_player.library.scan import scan_library


def cmd_scan(cfg: config.Config) -> None:
    if not cfg.library_path.is_dir():
        raise SystemExit(f"library folder not found: {cfg.library_path}")
    with closing(db.connect(cfg.db_path)) as conn:
        result = scan_library(conn, cfg.library_path)
    print(f"{result.found} songs found, {result.added} new, "
          f"{result.missing} missing, {len(result.errors)} unreadable")
    if not result.traversal_complete:
        raise SystemExit("library scan incomplete; missing-file detection was skipped")


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
    sub.add_parser("scan", help="index the library folder")
    sub.add_parser("play", help="start the terminal player")
    sub.add_parser("plays", help="rebuild plays from listening events and summarize them")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config.load(args.config)
    {"scan": cmd_scan, "play": cmd_play, "plays": cmd_plays}[args.command](cfg)


if __name__ == "__main__":
    main()
