import sqlite3
from collections.abc import Iterable, Mapping

COMPLETION_RATIO = 0.9
EARLY_SKIP_MS = 30_000
TERMINAL_TYPES = {"ended", "next", "previous", "error"}


def derive_plays(events: Iterable[Mapping]) -> list[dict]:
    """Summarize events, given in insertion order, into one row per play."""
    by_play: dict[str, list[Mapping]] = {}
    for event in events:
        by_play.setdefault(event["play_id"], []).append(event)
    return [_derive_play(play_id, play_events) for play_id, play_events in by_play.items()]


def rebuild_plays(conn: sqlite3.Connection) -> list[dict]:
    plays = derive_plays(conn.execute("SELECT * FROM events ORDER BY id"))
    with conn:
        conn.execute("DELETE FROM plays")
        if plays:
            columns = list(plays[0])
            conn.executemany(
                f"INSERT INTO plays ({', '.join(columns)}) "
                f"VALUES ({', '.join(':' + c for c in columns)})",
                plays,
            )
    return plays


def _derive_play(play_id: str, events: list[Mapping]) -> dict:
    start = events[0]
    if start["type"] != "start":
        raise ValueError(f"play {play_id} does not begin with a start event")

    liked = bool(start["liked"])
    listened = 0
    terminal = None
    for event in events[1:]:
        if terminal is not None:
            raise ValueError(f"play {play_id}: event {event['id']} follows its {terminal} event")
        if event["type"] == "start":
            raise ValueError(f"play {play_id}: second start event {event['id']}")
        if event["listened_total_ms"] < listened:
            raise ValueError(f"play {play_id}: listening counter decreased at event {event['id']}")
        listened = event["listened_total_ms"]
        if event["type"] in ("like", "unlike"):
            liked = event["type"] == "like"
        elif event["type"] in TERMINAL_TYPES:
            terminal = event["type"]

    completed = listened >= COMPLETION_RATIO * start["duration_ms"]
    skipped = terminal == "next" and not completed
    return {
        "play_id": play_id,
        "session_id": start["session_id"],
        "song_id": start["song_id"],
        "started_at": start["occurred_at"],
        "ended_at": events[-1]["occurred_at"],
        "reason": start["reason"],
        "duration_ms": start["duration_ms"],
        "listened_ms": listened,
        "terminal_event": terminal,
        "reached_end": terminal == "ended",
        "completed": completed,
        "early_skip": skipped and listened < EARLY_SKIP_MS,
        "late_skip": skipped and listened >= EARLY_SKIP_MS,
        "replayed": start["reason"] in ("previous", "restart"),
        "liked": liked,
        "censored": terminal in (None, "error"),
        "pick_id": start["pick_id"],
        "navigation_from_play_id": start["navigation_from_play_id"],
        "replay_of_play_id": start["replay_of_play_id"],
    }
