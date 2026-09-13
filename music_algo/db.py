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
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Preserve existing libraries created before artwork was stored.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(songs)")}
    with conn:
        for name, sql_type in (("cover_art", "BLOB"), ("cover_mime", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE songs ADD COLUMN {name} {sql_type}")
    return conn
