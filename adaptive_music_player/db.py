import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    artist       TEXT NOT NULL,
    album        TEXT NOT NULL,
    track_number INTEGER,
    duration_ms  INTEGER NOT NULL,
    cover_art    BLOB,
    cover_mime   TEXT,
    available    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS picks (
    id               TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    song_id          INTEGER NOT NULL REFERENCES songs(id),
    was_exploration  INTEGER NOT NULL,
    pick_probability REAL NOT NULL,
    predicted_score  REAL
);

CREATE TABLE IF NOT EXISTS events (
    id                      INTEGER PRIMARY KEY,
    occurred_at             TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    play_id                 TEXT NOT NULL,
    song_id                 INTEGER NOT NULL REFERENCES songs(id),
    type                    TEXT NOT NULL,
    position_ms             INTEGER NOT NULL,
    listened_total_ms       INTEGER NOT NULL,
    seek_from_ms            INTEGER,
    seek_to_ms              INTEGER,
    duration_ms             INTEGER,
    liked                   INTEGER,
    reason                  TEXT,
    navigation_from_play_id TEXT,
    replay_of_play_id       TEXT,
    pick_id                 TEXT REFERENCES picks(id)
);

-- A pick can start at most one play.
CREATE UNIQUE INDEX IF NOT EXISTS events_pick_id ON events(pick_id) WHERE pick_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_song_type ON events(song_id, type);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # Preserve existing libraries created before artwork was stored.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(songs)")}
    with conn:
        for name, sql_type in (("cover_art", "BLOB"), ("cover_mime", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE songs ADD COLUMN {name} {sql_type}")
    return conn
