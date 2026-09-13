import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    library_path: Path
    db_path: Path


def load(path: Path = Path("config.toml")) -> Config:
    if not path.exists():
        raise SystemExit(f"{path} not found; copy config.example.toml to {path} and edit it")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Config(
        library_path=Path(raw["library_path"]).expanduser(),
        db_path=Path(raw.get("db_path", "data/music.db")).expanduser(),
    )
