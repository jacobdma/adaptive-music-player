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

CREATE TABLE IF NOT EXISTS plays (
    play_id                 TEXT PRIMARY KEY,
    session_id              TEXT NOT NULL,
    song_id                 INTEGER NOT NULL REFERENCES songs(id),
    started_at              TEXT NOT NULL,
    ended_at                TEXT NOT NULL,
    reason                  TEXT NOT NULL,
    duration_ms             INTEGER NOT NULL,
    listened_ms             INTEGER NOT NULL,
    terminal_event          TEXT,
    reached_end             INTEGER NOT NULL,
    completed               INTEGER NOT NULL,
    early_skip              INTEGER NOT NULL,
    late_skip               INTEGER NOT NULL,
    replayed                INTEGER NOT NULL,
    liked                   INTEGER NOT NULL,
    censored                INTEGER NOT NULL,
    pick_id                 TEXT REFERENCES picks(id),
    navigation_from_play_id TEXT,
    replay_of_play_id       TEXT
);

CREATE TABLE IF NOT EXISTS song_features (
    song_id         INTEGER NOT NULL REFERENCES songs(id),
    source          TEXT NOT NULL,
    version         TEXT NOT NULL,
    status          TEXT NOT NULL,
    data            BLOB,
    error           TEXT,
    computed_at     TEXT NOT NULL,
    input_signature TEXT,
    PRIMARY KEY (song_id, source)
);
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
        feature_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(song_features)")
        }

        if "input_signature" not in feature_columns:
            conn.execute("ALTER TABLE song_features ADD COLUMN input_signature TEXT")
    return conn
