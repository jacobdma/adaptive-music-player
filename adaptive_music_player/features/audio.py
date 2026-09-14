import subprocess

import numpy as np


def decode(path: str, sample_rate: int) -> np.ndarray:
    """Decode a whole song to mono float32 samples."""
    process = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"],
        capture_output=True,
    )
    if process.returncode != 0:
        lines = process.stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(lines[-1] if lines else "ffmpeg failed")
    samples = np.frombuffer(process.stdout, dtype=np.float32)
    if samples.size == 0:
        raise ValueError("no audio decoded")
    return samples


def windows(samples: np.ndarray, count: int, length: int) -> list[np.ndarray]:
    """Up to `count` clips of `length` samples, evenly spaced across the song."""
    if samples.size <= length:
        return [samples]
    centers = (np.arange(count) + 0.5) / count * samples.size
    starts = np.clip((centers - length / 2).astype(int), 0, samples.size - length)
    return [samples[start:start + length] for start in np.unique(starts)]
