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

FEATURE_NAMES = (
    *(f"sound_{index}" for index in range(PCA_DIMENSIONS)), "tempo", "energy", "duration_min",
    "liked", "replays", "completion_rate", "early_skips", "plays_24h", "days_since_played",
    "artist_completion_rate", "taste_similarity", "previous_similarity", "tempo_difference",
    "hour_sin", "hour_cos",
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
            if song_id in self.vectors.rows:
                self.completions.append((song_id, _timestamp(play["ended_at"])))
                self._update_taste_points()

    def features(self, song_ids: Sequence[int], at: datetime, previous_song_id: int | None) -> np.ndarray:
        """Features per song from plays added so far."""
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
        if previous_song_id in vectors.rows:
            previous = vectors.rows[previous_song_id]
            previous_similarity = combined @ vectors.combined[previous]
            tempo_difference = np.abs(vectors.tempo[rows] - vectors.tempo[previous])
        else:
            previous_similarity = tempo_difference = np.zeros(len(rows))
        local = at.astimezone()
        angle = 2 * math.pi * (local.hour + local.minute / 60) / 24

        return np.column_stack([
            vectors.sound_pca[rows], vectors.tempo[rows], vectors.energy[rows],
            history_columns[:, :1], history_columns[:, 1:],
            taste, previous_similarity, tempo_difference,
            np.full(len(rows), math.sin(angle)), np.full(len(rows), math.cos(angle)),
        ])

    def _update_taste_points(self) -> None:
        """Weighted k-means over completed songs."""
        from sklearn.cluster import KMeans

        latest = self.completions[-1][1]
        weights: dict[int, float] = {}
        for song_id, ended in self.completions:
            weights[song_id] = weights.get(song_id, 0.0) + 0.5 ** ((latest - ended) / self.half_life_s)
        points = self.vectors.combined[[self.vectors.rows[song_id] for song_id in weights]]
        clusters = min(MAX_TASTE_POINTS, len(np.unique(points, axis=0)))
        kmeans = KMeans(n_clusters=clusters, n_init=1, random_state=0)
        centers = kmeans.fit(points, sample_weight=list(weights.values())).cluster_centers_
        norms = np.linalg.norm(centers, axis=1)
        self.taste_points = centers[norms > 0] / norms[norms > 0, None]


def training_rows(history: History, plays: Sequence[Mapping]) -> tuple[np.ndarray, np.ndarray, list[datetime]]:
    """Add plays to history."""
    rows, labels, times = [], [], []
    previous_song_id = None
    for play in plays:
        started = datetime.fromisoformat(play["started_at"])
        if not play["censored"] and play["song_id"] in history.vectors.rows:
            rows.append(history.features([play["song_id"]], started, previous_song_id)[0])
            labels.append(int(play["completed"]))
            times.append(started)
        history.add(play)
        previous_song_id = play["song_id"]
    return np.array(rows).reshape(len(rows), len(FEATURE_NAMES)), np.array(labels), times


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()
