import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np

from adaptive_music_player.events.plays import derive_plays
from adaptive_music_player.model.features import DAY_S, FEATURE_NAMES, History, training_rows
from adaptive_music_player.model.taste import INTERACTIONS, TasteModel, attention_weight
from adaptive_music_player.model.vectors import load_vectors

COOLDOWN_HOURS = 3
EXPLORATION_START = 0.5
EXPLORATION_MIN = 0.1
EXPLORATION_DECAY = 0.99
EXPLORATION_CLUSTERS = 12
UNIFORM_EXPLORATION_SHARE = 0.1
TOP_CANDIDATES = 5
COLD_START_CANDIDATES = 25
SESSION_BLEND = 0.35
SKIP_PENALTY = 0.3


@dataclass(frozen=True)
class Pick:
    id: str
    song_id: int
    was_exploration: bool
    pick_probability: float
    predicted_score: float | None


class Recommender:
    """Picks the next song."""

    def __init__(self, conn: sqlite3.Connection, rng: np.random.Generator | None = None) -> None:
        self.conn = conn
        self.rng = rng or np.random.default_rng()
        self.songs = {row["id"]: row for row in conn.execute("SELECT id, path, artist, duration_ms FROM songs")}
        self.vectors = load_vectors(conn)
        self.model = TasteModel()
        self.last_event_id = 0
        if self.vectors is None:
            return

        from sklearn.cluster import KMeans

        clusters = min(EXPLORATION_CLUSTERS, len(np.unique(self.vectors.combined, axis=0)))
        self.clusters = KMeans(n_clusters=clusters, n_init=1, random_state=0).fit_predict(self.vectors.combined)
        self._reset_learning()
        self._sync()

    def pick(self, current_song_id: int | None) -> Pick | None:
        if self.vectors is None:
            return None
        self._sync()
        now = datetime.now(UTC)
        candidates = self._eligible(current_song_id, now)
        if not candidates:
            return None
        pick = self._choose(candidates, current_song_id, now)
        with self.conn:
            self.conn.execute(
                "INSERT INTO picks (id, created_at, song_id, was_exploration, pick_probability, "
                "predicted_score) VALUES (?, ?, ?, ?, ?, ?)",
                (pick.id, now.isoformat(timespec="milliseconds"), pick.song_id,
                 int(pick.was_exploration), pick.pick_probability, pick.predicted_score),
            )
        return pick

    def distributions(self, candidates: list[int], current_song_id: int | None,
                      now: datetime) -> tuple[float, np.ndarray, np.ndarray, np.ndarray]:
        """Exploration rate, explore and exploit probabilities, and predicted scores per candidate."""
        epsilon = max(EXPLORATION_MIN, EXPLORATION_START * EXPLORATION_DECAY ** self.play_count)
        if not candidates:
            return epsilon, np.empty(0), np.empty(0), np.empty(0)
        ids = np.array(candidates)
        rows = np.array([self.vectors.rows[song_id] for song_id in candidates])

        clusters = self.clusters[rows]
        recent_plays = np.zeros(self.clusters.max() + 1)
        for song_id, stats in self.history.song_history.items():
            if song_id in self.vectors.rows:
                recent = sum(start >= now.timestamp() - DAY_S for start in stats.start_times)
                recent_plays[self.clusters[self.vectors.rows[song_id]]] += recent
        present = np.unique(clusters)
        quietest = present[recent_plays[present] == recent_plays[present].min()]
        explore = np.zeros(len(ids))
        for cluster in quietest:
            members = clusters == cluster
            explore[members] = 1 / (len(quietest) * members.sum())
        explore = (1 - UNIFORM_EXPLORATION_SHARE) * explore + UNIFORM_EXPLORATION_SHARE / len(ids)

        features = self.history.features(candidates, now, current_song_id)
        if self.model.trained:
            scores = self.model.predict(features)
            predicted = scores
        else:
            scores = _cold_start_scores(features)
            predicted = np.full(len(ids), self.model.prior)

        exploit = np.zeros(len(ids))
        limit = TOP_CANDIDATES if self.model.trained else COLD_START_CANDIDATES
        top = np.lexsort((ids, -scores))[:limit]
        top = top[scores[top] > 0]
        if len(top) and not np.allclose(scores, scores[0], rtol=0, atol=1e-12):
            exploit[top] = scores[top] / scores[top].sum()
        else:
            exploit[:] = 1 / len(ids)
        return epsilon, explore, exploit, predicted

    def _choose(self, candidates: list[int], current_song_id: int | None, now: datetime) -> Pick:
        epsilon, explore, exploit, predicted = self.distributions(candidates, current_song_id, now)
        explored = bool(self.rng.random() < epsilon)
        index = int(self.rng.choice(len(candidates), p=explore if explored else exploit))
        return Pick(id=str(uuid4()), song_id=candidates[index], was_exploration=explored,
                    pick_probability=float(epsilon * explore[index] + (1 - epsilon) * exploit[index]),
                    predicted_score=float(predicted[index]))

    def _eligible(self, current_song_id: int | None, now: datetime) -> list[int]:
        """Playable songs other than the current one."""
        available = {row[0] for row in self.conn.execute("SELECT id FROM songs WHERE available = 1")}
        playable = [int(song_id) for song_id in self.vectors.song_ids if song_id in available]
        # Validate before sampling so the logged probability uses the actual candidate set.
        missing = {song_id for song_id in playable if not Path(self.songs[song_id]["path"]).is_file()}
        if missing:
            with self.conn:
                self.conn.executemany("UPDATE songs SET available = 0 WHERE id = ?",
                                      [(song_id,) for song_id in missing])
            playable = [song_id for song_id in playable if song_id not in missing]
        others = [song_id for song_id in playable if song_id != current_song_id]
        cutoff = now.timestamp() - COOLDOWN_HOURS * 3600
        rested = [song_id for song_id in others
                  if not (stats := self.history.song_history.get(song_id)) or stats.start_times[-1] < cutoff]
        return rested or others or playable

    def _reset_learning(self) -> None:
        self.history = History(self.vectors, self.songs, self.model.half_life_days)
        self.play_count = 0
        self.previous_song_id: int | None = None
        self.rows: list[np.ndarray] = []
        self.labels: list[int] = []
        self.times: list[datetime] = []
        self.attention: list[float] = []
        self.last_interaction: float | None = None
        self._unfinished_play_ids: set[str] = set()

    def _sync(self) -> None:
        events = self.conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id",
                                   (self.last_event_id,)).fetchall()
        if not events:
            return
        if any(event["play_id"] in self._unfinished_play_ids for event in events):
            events = self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()
            self._reset_learning()
        interaction_at_end: dict[str, float | None] = {}
        for event in events:
            if event["type"] in INTERACTIONS:
                self.last_interaction = datetime.fromisoformat(event["occurred_at"]).timestamp()
            interaction_at_end[event["play_id"]] = self.last_interaction

        plays = derive_plays(events)
        rows, used = training_rows(self.history, plays, self.previous_song_id)
        self.rows.extend(rows)
        self.labels.extend(int(play["completed"]) for play in used)
        self.times.extend(datetime.fromisoformat(play["started_at"]) for play in used)
        self.attention.extend(attention_weight(play, interaction_at_end[play["play_id"]]) for play in used)
        self._unfinished_play_ids.update(play["play_id"] for play in plays if play["terminal_event"] is None)
        self.play_count += len(plays)
        self.previous_song_id = plays[-1]["song_id"]
        self.model.fit(np.array(self.rows).reshape(-1, len(FEATURE_NAMES)), np.array(self.labels),
                       self.times, datetime.now(UTC), np.array(self.attention))
        self.last_event_id = events[-1]["id"]


def _cold_start_scores(features: np.ndarray) -> np.ndarray:
    """Use observed taste and the current session while the learned ranker is warming up."""
    def similarity(name: str) -> np.ndarray:
        return np.clip(features[:, FEATURE_NAMES.index(name)], 0, 1)

    positive = np.maximum(similarity("taste_similarity"), similarity("liked_similarity"))
    negative = ((1 - SESSION_BLEND) * similarity("skip_similarity")
                + SESSION_BLEND * similarity("session_skip_similarity"))
    return (1 + (1 - SESSION_BLEND) * positive + SESSION_BLEND * similarity("session_similarity")
            - SKIP_PENALTY * negative)
