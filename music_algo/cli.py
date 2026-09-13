import argparse
import logging
from contextlib import closing
from pathlib import Path

from music_algo import config, db
from music_algo.library.scan import scan_library


def cmd_scan(cfg: config.Config) -> None:
    if not cfg.library_path.is_dir():
        raise SystemExit(f"library folder not found: {cfg.library_path}")
    with closing(db.connect(cfg.db_path)) as conn:
        result = scan_library(conn, cfg.library_path)
    print(f"{result.found} songs found, {result.added} new, "
          f"{result.missing} missing, {len(result.errors)} unreadable")
    if not result.traversal_complete:
        raise SystemExit("library scan incomplete; missing-file detection was skipped")


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-algo")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="index the library folder")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config.load(args.config)
    {"scan": cmd_scan}[args.command](cfg)


if __name__ == "__main__":
    main()
