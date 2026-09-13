import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class Pick:
    id: str
    song_id: int
    was_exploration: bool
    pick_probability: float
    predicted_score: float | None


def pick_random(conn: sqlite3.Connection, current_song_id: int | None = None,
                rng: random.Random | None = None) -> Pick | None:
    """Uniform pick over available songs, avoiding the current song when possible.

    Stands in for the taste-model picker until step 4. A uniform pick is pure
    exploration with no prediction. Returns None when nothing is playable.
    """
    rng = rng or random.Random()
    rows = conn.execute("SELECT id, path FROM songs WHERE available = 1").fetchall()
    candidates = [row for row in rows if row["id"] != current_song_id] or rows

    with conn:
        while candidates:
            choice = rng.choice(candidates)
            if Path(choice["path"]).is_file():
                break
            conn.execute("UPDATE songs SET available = 0 WHERE id = ?", (choice["id"],))
            candidates.remove(choice)
        else:
            return None

        pick = Pick(id=str(uuid4()), song_id=choice["id"], was_exploration=True,
                    pick_probability=1 / len(candidates), predicted_score=None)
        conn.execute(
            "INSERT INTO picks (id, created_at, song_id, was_exploration, pick_probability, "
            "predicted_score) VALUES (?, ?, ?, ?, ?, ?)",
            (pick.id, datetime.now(UTC).isoformat(timespec="milliseconds"), pick.song_id,
             int(pick.was_exploration), pick.pick_probability, pick.predicted_score),
        )
    return pick
