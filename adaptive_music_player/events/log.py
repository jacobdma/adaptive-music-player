import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal, get_args

EventType = Literal["start", "progress", "pause", "resume", "seek", "next", "previous",
                    "like", "unlike", "ended", "error"]
Reason = Literal["automatic", "previous", "restart"]

SEEK_FIELDS = ("seek_from_ms", "seek_to_ms")
NAVIGATION_FIELDS = ("navigation_from_play_id", "replay_of_play_id")
START_FIELDS = ("duration_ms", "liked", "reason", *NAVIGATION_FIELDS, "pick_id")


@dataclass(frozen=True)
class Event:
    session_id: str
    play_id: str
    song_id: int
    type: EventType
    position_ms: int
    listened_total_ms: int
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    seek_from_ms: int | None = None
    seek_to_ms: int | None = None
    duration_ms: int | None = None
    liked: bool | None = None
    reason: Reason | None = None
    navigation_from_play_id: str | None = None
    replay_of_play_id: str | None = None
    pick_id: str | None = None

    def __post_init__(self) -> None:
        if self.type not in get_args(EventType):
            raise ValueError(f"unknown event type {self.type!r}")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        for name in ("position_ms", "listened_total_ms", *SEEK_FIELDS, "duration_ms"):
            if (value := getattr(self, name)) is not None and value < 0:
                raise ValueError(f"{name} must not be negative")

        if self.type == "seek":
            self._require(SEEK_FIELDS)
        else:
            self._forbid(SEEK_FIELDS)
        if self.type != "start":
            self._forbid(START_FIELDS)
            return

        self._require(("duration_ms", "liked", "reason"))
        if self.listened_total_ms != 0:
            raise ValueError("start events must have listened_total_ms = 0")
        if self.reason == "automatic":
            self._require(("pick_id",))
            self._forbid(NAVIGATION_FIELDS)
        elif self.reason in get_args(Reason):
            self._require(NAVIGATION_FIELDS)
            self._forbid(("pick_id",))
        else:
            raise ValueError(f"unknown start reason {self.reason!r}")

    def _require(self, names: tuple[str, ...]) -> None:
        if missing := [n for n in names if getattr(self, n) is None]:
            raise ValueError(f"{self.type} events require {', '.join(missing)}")

    def _forbid(self, names: tuple[str, ...]) -> None:
        if present := [n for n in names if getattr(self, n) is not None]:
            raise ValueError(f"{self.type} events must not include {', '.join(present)}")


def record_event(conn: sqlite3.Connection, event: Event) -> None:
    row = asdict(event)
    row["occurred_at"] = event.occurred_at.astimezone(UTC).isoformat(timespec="milliseconds")
    with conn:
        conn.execute(
            f"INSERT INTO events ({', '.join(row)}) VALUES ({', '.join(':' + n for n in row)})",
            row,
        )


def liked_state(conn: sqlite3.Connection, song_id: int) -> bool:
    row = conn.execute(
        "SELECT type FROM events WHERE song_id = ? AND type IN ('like', 'unlike') "
        "ORDER BY id DESC LIMIT 1",
        (song_id,),
    ).fetchone()
    return row is not None and row["type"] == "like"
