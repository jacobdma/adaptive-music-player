from collections.abc import Mapping
from datetime import datetime

import numpy as np

from adaptive_music_player.model.features import DAY_S

MIN_TRAINING_PLAYS = 30
HALF_LIFE_DAYS = 30.0
REGULARIZATION_C = 1.0
ACTIVE_MINUTES = 30
ATTENTION_HALF_LIFE_MINUTES = 30
ATTENTION_FLOOR = 0.3
INTERACTIONS = frozenset({"pause", "resume", "seek", "next", "previous", "like", "unlike"})


def attention_weight(play: Mapping, last_interaction: float | None) -> float:
    """Completions count less the longer you had gone without interacting when they ended."""
    if not play["completed"]:
        return 1.0
    if last_interaction is None:
        return ATTENTION_FLOOR
    silent_minutes = (datetime.fromisoformat(play["ended_at"]).timestamp() - last_interaction) / 60
    fade = max(0.0, silent_minutes - ACTIVE_MINUTES) / ATTENTION_HALF_LIFE_MINUTES
    return max(ATTENTION_FLOOR, 0.5 ** fade)


class TasteModel:
    """Predicts the chance a play is completed."""

    def __init__(self, half_life_days: float = HALF_LIFE_DAYS, c: float = REGULARIZATION_C) -> None:
        self.half_life_days = half_life_days
        self.c = c
        self.prior = 0.5
        self.pipeline = None

    @property
    def trained(self) -> bool:
        return self.pipeline is not None

    def fit(self, rows: np.ndarray, labels: np.ndarray, times: list[datetime], now: datetime,
            attention: np.ndarray | None = None) -> None:
        ages = np.array([max(0.0, (now - time).total_seconds() / DAY_S) for time in times])
        weights = 0.5 ** (ages / self.half_life_days)
        if attention is not None:
            weights = weights * attention
        self.prior = float((weights @ labels + 1) / (weights.sum() + 2))
        self.pipeline = None
        if (len(labels) < MIN_TRAINING_PLAYS or not weights[labels == 0].sum()
                or not weights[labels == 1].sum() or not np.any(np.ptp(rows, axis=0) > 0)):
            return

        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        pipeline = make_pipeline(StandardScaler(), LogisticRegression(C=self.c, max_iter=1000))
        pipeline.fit(rows, labels, logisticregression__sample_weight=weights)
        self.pipeline = pipeline

    def predict(self, rows: np.ndarray) -> np.ndarray:
        if self.pipeline is None:
            return np.full(len(rows), self.prior)
        return self.pipeline.predict_proba(rows)[:, 1]
