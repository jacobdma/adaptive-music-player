import sqlite3
from dataclasses import dataclass

import numpy as np

from adaptive_music_player.features.extract import VERSIONS

TEMPO_ENERGY_WEIGHT = 0.5
PCA_DIMENSIONS = 16


@dataclass(frozen=True)
class SongVectors:
    song_ids: np.ndarray
    combined: np.ndarray
    sound_pca: np.ndarray
    tempo: np.ndarray
    energy: np.ndarray
    rows: dict[int, int]


def load_vectors(conn: sqlite3.Connection) -> SongVectors | None:
    """Vectors for available songs with current analysis.

    `combined` is L2-normalized for cosine similarity: CLAP centered on the library mean,
    plus standardized tempo and energy at TEMPO_ENERGY_WEIGHT of CLAP's total influence.
    """
    rows = conn.execute(
        "SELECT s.id, c.data AS clap, l.data AS librosa FROM songs s "
        "JOIN song_features c ON c.song_id = s.id AND c.source = 'clap' AND c.status = 'ok' AND c.version = ? "
        "JOIN song_features l ON l.song_id = s.id AND l.source = 'librosa' AND l.status = 'ok' AND l.version = ? "
        "WHERE s.available = 1 ORDER BY s.id",
        (VERSIONS["clap"], VERSIONS["librosa"]),
    ).fetchall()
    if not rows:
        return None

    song_ids = np.array([row["id"] for row in rows])
    clap = np.stack([np.frombuffer(row["clap"], dtype=np.float32) for row in rows])
    tempo_energy = np.stack([np.frombuffer(row["librosa"], dtype=np.float32) for row in rows])

    clap = clap - clap.mean(axis=0)
    clap_block = clap / (np.sqrt((clap ** 2).sum(axis=1).mean()) or 1.0)
    spread = tempo_energy.std(axis=0)
    spread[spread == 0] = 1.0
    tempo_energy_block = (tempo_energy - tempo_energy.mean(axis=0)) / spread / np.sqrt(tempo_energy.shape[1])

    combined = np.hstack([clap_block, TEMPO_ENERGY_WEIGHT * tempo_energy_block])
    norms = np.linalg.norm(combined, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    _, _, components = np.linalg.svd(clap, full_matrices=False)
    projected = clap @ components[:PCA_DIMENSIONS].T
    sound_pca = np.zeros((len(rows), PCA_DIMENSIONS), dtype=projected.dtype)
    sound_pca[:, :projected.shape[1]] = projected

    return SongVectors(
        song_ids=song_ids,
        combined=combined / norms,
        sound_pca=sound_pca,
        tempo=tempo_energy[:, 0],
        energy=tempo_energy[:, 1],
        rows={int(song_id): index for index, song_id in enumerate(song_ids)},
    )
