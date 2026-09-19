import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from adaptive_music_player.model.vectors import PCA_DIMENSIONS, SongVectors

MAX_TASTE_POINTS = 3
DAYS_SINCE_PLAYED_CAP = 90.0
DAY_S = 86_400
SESSION_HALF_LIFE_MINUTES = 20
SESSION_MAX_MINUTES = 180

FEATURE_NAMES = (
    *(f"sound_{index}" for index in range(PCA_DIMENSIONS)), "tempo", "energy", "duration_min",
    "liked", "replays", "completion_rate", "early_skips", "plays_24h", "days_since_played",
    "artist_completion_rate", "taste_similarity", "liked_similarity", "skip_similarity", "session_similarity",
    "session_skip_similarity", "previous_similarity", "tempo_difference", "hour_sin", "hour_cos",
    "energy_x_hour_sin", "energy_x_hour_cos", "tempo_x_hour_sin", "tempo_x_hour_cos",
    "taste_x_hour_sin", "taste_x_hour_cos",
)


@dataclass
class _SongHistory:
    finalized: int = 0
    completed: int = 0
    replays: int = 0
    early_skips: int = 0
    liked: bool = False
    start_times: list[float] = field(default_factory=list)


class History:
    """Listening history added in play order."""

    def __init__(self, vectors: SongVectors, songs: Mapping[int, Mapping], half_life_days: float) -> None:
        self.vectors = vectors
        self.songs = songs
        self.half_life_s = half_life_days * DAY_S
        self.song_history: dict[int, _SongHistory] = {}
        self.artist_counts: dict[str, list[int]] = {}
        self.completions: list[tuple[int, float]] = []
        self.taste_points = np.empty((0, vectors.combined.shape[1]))
        self.skips: list[tuple[int, float]] = []
        self.skip_point = np.zeros(vectors.combined.shape[1])
        self.session: list[tuple[float, int, bool]] = []
        self.energy_z = _standardize(vectors.energy)
        self.tempo_z = _standardize(vectors.tempo)

    def add(self, play: Mapping) -> None:
        song_id = play["song_id"]
        stats = self.song_history.setdefault(song_id, _SongHistory())
        stats.start_times.append(_timestamp(play["started_at"]))
        stats.liked = bool(play["liked"])
        stats.replays += bool(play["replayed"])
        if play["censored"]:
            return

        counts = self.artist_counts.setdefault(self.songs[song_id]["artist"], [0, 0])
        stats.finalized += 1
        counts[0] += 1
        stats.early_skips += bool(play["early_skip"])
        if play["completed"]:
            stats.completed += 1
            counts[1] += 1

        row = self.vectors.rows.get(song_id)
        if row is None:
            return
        ended = _timestamp(play["ended_at"])
        if play["completed"]:
            self.completions.append((song_id, ended))
            self._update_taste_points()
        if play["early_skip"]:
            self.skips.append((song_id, ended))
            weights = self._recency_weights(self.skips)
            self.skip_point = _direction(sum(weight * self.vectors.combined[self.vectors.rows[skipped]]
                                             for skipped, weight in weights.items()))
        if play["early_skip"] or play["completed"] or play["liked"]:
            self.session = [entry for entry in self.session if ended - entry[0] <= SESSION_MAX_MINUTES * 60]
            self.session.append((ended, row, bool(play["early_skip"])))

    def features(self, song_ids: Sequence[int], at: datetime, previous_song_id: int | None) -> np.ndarray:
        """Features per song from plays added so far."""
        if not len(song_ids):
            return np.empty((0, len(FEATURE_NAMES)))
        vectors = self.vectors
        rows = np.array([vectors.rows[song_id] for song_id in song_ids])
        now = at.timestamp()
        empty = _SongHistory()

        history_columns = []
        for song_id in song_ids:
            stats = self.song_history.get(song_id, empty)
            artist_finalized, artist_completed = self.artist_counts.get(self.songs[song_id]["artist"], (0, 0))
            history_columns.append((
                self.songs[song_id]["duration_ms"] / 60_000,
                stats.liked,
                stats.replays,
                (stats.completed + 1) / (stats.finalized + 2),
                stats.early_skips,
                len(stats.start_times) - bisect.bisect_left(stats.start_times, now - DAY_S),
                min((now - stats.start_times[-1]) / DAY_S, DAYS_SINCE_PLAYED_CAP)
                if stats.start_times else DAYS_SINCE_PLAYED_CAP,
                (artist_completed + 1) / (artist_finalized + 2),
            ))
        history_columns = np.array(history_columns, dtype=np.float64)

        combined = vectors.combined[rows]
        taste = ((combined @ self.taste_points.T).max(axis=1) if len(self.taste_points)
                 else np.zeros(len(rows)))
        liked_rows = [vectors.rows[song_id] for song_id, stats in self.song_history.items()
                      if stats.liked and song_id in vectors.rows]
        liked = ((combined @ vectors.combined[liked_rows].T).max(axis=1) if liked_rows
                 else np.zeros(len(rows)))
        session_liked, session_skipped = self._session_profiles(now)
        if previous_song_id in vectors.rows:
            previous = vectors.rows[previous_song_id]
            previous_similarity = combined @ vectors.combined[previous]
            tempo_difference = np.abs(vectors.tempo[rows] - vectors.tempo[previous])
        else:
            previous_similarity = tempo_difference = np.zeros(len(rows))
        local = at.astimezone()
        angle = 2 * math.pi * (local.hour + local.minute / 60) / 24
        hour_sin, hour_cos = math.sin(angle), math.cos(angle)
        time_interactions = [values * trig for values in (self.energy_z[rows], self.tempo_z[rows], taste)
                             for trig in (hour_sin, hour_cos)]

        return np.column_stack([
            vectors.sound_pca[rows], vectors.tempo[rows], vectors.energy[rows],
            history_columns[:, :1], history_columns[:, 1:],
            taste, liked, combined @ self.skip_point, combined @ session_liked, combined @ session_skipped,
            previous_similarity, tempo_difference,
            np.full(len(rows), hour_sin), np.full(len(rows), hour_cos), *time_interactions,
        ])

    def _session_profiles(self, now: float) -> tuple[np.ndarray, np.ndarray]:
        """Directions of recently finished-or-liked and early-skipped songs."""
        profiles = []
        for skipped in (False, True):
            total = np.zeros(self.vectors.combined.shape[1])
            support = 0.0
            for ended, row, was_skipped in self.session:
                if was_skipped == skipped and 0 <= now - ended <= SESSION_MAX_MINUTES * 60:
                    weight = 0.5 ** ((now - ended) / 60 / SESSION_HALF_LIFE_MINUTES)
                    total += weight * self.vectors.combined[row]
                    support += weight
            profiles.append(_direction(total) * min(1.0, support))
        return profiles[0], profiles[1]

    def _recency_weights(self, entries: list[tuple[int, float]]) -> dict[int, float]:
        latest = entries[-1][1]
        weights: dict[int, float] = {}
        for song_id, ended in entries:
            weights[song_id] = weights.get(song_id, 0.0) + 0.5 ** ((latest - ended) / self.half_life_s)
        return weights

    def _update_taste_points(self) -> None:
        """Weighted k-means over completed songs."""
        from sklearn.cluster import KMeans

        weights = self._recency_weights(self.completions)
        points = self.vectors.combined[[self.vectors.rows[song_id] for song_id in weights]]
        clusters = min(MAX_TASTE_POINTS, len(np.unique(points, axis=0)))
        kmeans = KMeans(n_clusters=clusters, n_init=1, random_state=0)
        centers = kmeans.fit(points, sample_weight=list(weights.values())).cluster_centers_
        norms = np.linalg.norm(centers, axis=1)
        self.taste_points = centers[norms > 0] / norms[norms > 0, None]


def training_rows(history: History, plays: Sequence[Mapping],
                  previous_song_id: int | None = None) -> tuple[np.ndarray, list[Mapping]]:
    """Add plays to history, returning features for each finalized play and the plays they describe."""
    rows, used = [], []
    for play in plays:
        if not play["censored"] and play["song_id"] in history.vectors.rows:
            started = datetime.fromisoformat(play["started_at"])
            rows.append(history.features([play["song_id"]], started, previous_song_id)[0])
            used.append(play)
        history.add(play)
        previous_song_id = play["song_id"]
    return np.array(rows).reshape(len(rows), len(FEATURE_NAMES)), used


def _standardize(values: np.ndarray) -> np.ndarray:
    spread = values.std()
    return (values - values.mean()) / spread if spread > 0 else np.zeros_like(values)


def _direction(vector: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 0 else vector


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()
