import logging
import os
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version

import numpy as np

from adaptive_music_player.features.audio import decode, windows

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
ANALYSIS_SAMPLE_RATE = 22_050
CLIP_SECONDS = 10
CLIPS_PER_SONG = 6
SONGS_PER_BATCH = 8
WORKERS = min(8, os.cpu_count() or 1)
CLAP_MODEL = "laion/larger_clap_music"
CLAP_REVISION = "a0b4534a14f58e20944452dff00a22a06ce629d1"

VERSIONS = {
    "librosa": f"librosa-{version('librosa')};full;{ANALYSIS_SAMPLE_RATE}hz;tempo,rms",
    "clap": f"{CLAP_MODEL}@{CLAP_REVISION};{CLIPS_PER_SONG}x{CLIP_SECONDS}s;{SAMPLE_RATE}hz;mean",
}

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class ExtractResult:
    analyzed: int = 0
    up_to_date: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class SongAnalysis:
    song: sqlite3.Row
    sources: tuple[str, ...]
    input_signature: str
    features: dict[str, np.ndarray] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    clips: list[np.ndarray] = field(default_factory=list)


class ClapEmbedder:
    def __init__(self) -> None:
        from huggingface_hub import constants, try_to_load_from_cache

        if isinstance(try_to_load_from_cache(CLAP_MODEL, "pytorch_model.bin", revision=CLAP_REVISION), str):
            # Once cached, stay offline; otherwise transformers checks the Hub for converted weights.
            constants.HF_HUB_OFFLINE = True

        import torch
        from transformers import ClapFeatureExtractor, ClapModel
        from transformers.utils import logging as transformers_logging

        transformers_logging.set_verbosity_error()
        transformers_logging.disable_progress_bar()
        self.torch = torch
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.extractor = ClapFeatureExtractor.from_pretrained(CLAP_MODEL, revision=CLAP_REVISION)
        self.model = ClapModel.from_pretrained(CLAP_MODEL, revision=CLAP_REVISION, use_safetensors=False)
        self.model.to(self.device).eval()

    def embed(self, clips: list[np.ndarray]) -> np.ndarray:
        """One L2-normalized embedding per clip."""
        inputs = self.extractor(clips, sampling_rate=SAMPLE_RATE, max_length=CLIP_SECONDS * SAMPLE_RATE,
                                padding="pad", return_tensors="pt")
        with self.torch.no_grad():
            output = self.model.get_audio_features(
                **{name: value.to(self.device) for name, value in inputs.items()})
        features = output if isinstance(output, self.torch.Tensor) else output.pooler_output
        return self.torch.nn.functional.normalize(features, dim=-1).cpu().numpy()


def _input_signature(path: str) -> str:
    info = os.stat(path)
    return f"{info.st_size}:{info.st_mtime_ns}"


def _cached_signature(signature: str | None) -> str | None:
    # Reuse results saved with the previous dev:ino:size:mtime:ctime format.
    if signature is not None:
        parts = signature.split(":")
        if len(parts) == 5:
            return ":".join(parts[2:4])
    return signature


def pending_songs(conn: sqlite3.Connection) -> tuple[list[SongAnalysis], int]:
    """Available songs missing a current, successful result for any source."""
    rows = conn.execute("SELECT id, path, title, artist FROM songs WHERE available = 1 "
                        "ORDER BY id").fetchall()
    current = {
        (row["song_id"], row["source"]): _cached_signature(row["input_signature"])
        for row in conn.execute("SELECT song_id, source, version, status, input_signature "
                                "FROM song_features WHERE data IS NOT NULL")
        if row["status"] == "ok" and VERSIONS.get(row["source"]) == row["version"]
    }
    pending = []
    up_to_date = 0
    for row in rows:
        try:
            signature = _input_signature(row["path"])
        except OSError as exc:
            log.warning("skipping analysis for %s: cannot inspect file: %s", row["path"], exc)
            continue
        sources = tuple(source for source in VERSIONS
                        if current.get((row["id"], source)) != signature)
        if sources:
            pending.append(SongAnalysis(row, sources, signature))
        else:
            up_to_date += 1
    return pending, up_to_date


def extract_features(conn: sqlite3.Connection, embedder: ClapEmbedder | None = None,
                     on_progress: ProgressCallback | None = None) -> ExtractResult:
    report = on_progress or (lambda done, total, label: None)
    songs, up_to_date = pending_songs(conn)
    result = ExtractResult(up_to_date=up_to_date)
    if any("clap" in song.sources for song in songs) and embedder is None:
        embedder = ClapEmbedder()
    if any("librosa" in song.sources for song in songs):
        # Compile librosa's numba functions once, before worker threads race to compile them.
        _tempo_and_energy(np.random.default_rng(0).standard_normal(5 * SAMPLE_RATE).astype(np.float32))

    with ThreadPoolExecutor(WORKERS) as pool:
        for batch_start in range(0, len(songs), SONGS_PER_BATCH):
            batch = songs[batch_start:batch_start + SONGS_PER_BATCH]
            futures = [pool.submit(_prepare, analysis) for analysis in batch]
            clips: list[np.ndarray] = []
            owners: list[int] = []
            for offset, (analysis, future) in enumerate(zip(batch, futures)):
                song = analysis.song
                report(batch_start + offset + 1, len(songs), f"{song['artist']} — {song['title']}")
                try:
                    future.result()
                except Exception as exc:
                    _fail(analysis, analysis.sources, exc)
                clips.extend(analysis.clips)
                owners.extend([song["id"]] * len(analysis.clips))

            if clips:
                clap_songs = [analysis for analysis in batch if analysis.clips]
                try:
                    embeddings = embedder.embed(clips)
                except Exception as exc:
                    for analysis in clap_songs:
                        _fail(analysis, ("clap",), exc)
                else:
                    owner_ids = np.array(owners)
                    for analysis in clap_songs:
                        mean = embeddings[owner_ids == analysis.song["id"]].mean(axis=0)
                        analysis.features["clap"] = mean / np.linalg.norm(mean)
            clips.clear()
            for analysis in batch:
                analysis.clips.clear()

            # Keep all file inspection and computation outside the write transaction.
            inspected = []
            for analysis in batch:
                try:
                    signature = _input_signature(analysis.song["path"])
                except OSError as exc:
                    log.warning("skipping analysis for %s: cannot inspect file: %s",
                                analysis.song["path"], exc)
                    continue
                if signature != analysis.input_signature:
                    _fail(analysis, analysis.sources,
                          ValueError("file changed during analysis; run scan again"))
                inspected.append(analysis)

            with conn:
                for analysis in inspected:
                    for source in analysis.sources:
                        _store(conn, analysis.song["id"], source, analysis.features.get(source),
                               analysis.input_signature, error=analysis.errors.get(source))
            for analysis in inspected:
                if analysis.errors:
                    message = "; ".join(f"{source}: {error}" for source, error in analysis.errors.items())
                    log.warning("cannot analyze %s: %s", analysis.song["path"], message)
                    result.failed.append((analysis.song["path"], message))
                else:
                    result.analyzed += 1
    return result


def _prepare(analysis: SongAnalysis) -> None:
    audio = decode(analysis.song["path"], SAMPLE_RATE)
    if "librosa" in analysis.sources:
        try:
            analysis.features["librosa"] = _tempo_and_energy(audio)
        except Exception as exc:
            _fail(analysis, ("librosa",), exc)
    if "clap" in analysis.sources:
        try:
            analysis.clips = windows(audio, CLIPS_PER_SONG, CLIP_SECONDS * SAMPLE_RATE)
        except Exception as exc:
            _fail(analysis, ("clap",), exc)


def _tempo_and_energy(audio: np.ndarray) -> np.ndarray:
    import librosa

    audio = librosa.resample(audio, orig_sr=SAMPLE_RATE, target_sr=ANALYSIS_SAMPLE_RATE, res_type="soxr_lq")
    tempo, _ = librosa.beat.beat_track(y=audio, sr=ANALYSIS_SAMPLE_RATE)
    energy = librosa.feature.rms(y=audio).mean()
    return np.array([np.atleast_1d(tempo)[0], energy], dtype=np.float32)


def _store(conn: sqlite3.Connection, song_id: int, source: str, data: np.ndarray | None,
           input_signature: str | None, error: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO song_features "
        "(song_id, source, version, status, data, error, computed_at, input_signature) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (song_id, source, VERSIONS[source], "failed" if error is not None else "ok",
         None if data is None else data.astype(np.float32).tobytes(), error,
         datetime.now(UTC).isoformat(timespec="seconds"), input_signature),
    )


def _fail(analysis: SongAnalysis, sources: tuple[str, ...], exc: Exception) -> None:
    for source in sources:
        analysis.features.pop(source, None)
        analysis.errors[source] = str(exc) or type(exc).__name__
