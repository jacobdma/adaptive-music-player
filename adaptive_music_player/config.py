import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# The checkout this package lives in, so an installed `amp` finds config.toml
# and data/ no matter which folder the command is run from.
PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_VAR = "AMP_CONFIG"


@dataclass(frozen=True)
class Config:
    library_path: Path
    db_path: Path


def find(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    local = Path("config.toml")
    return local if local.exists() else PROJECT_DIR / "config.toml"


def load(path: Path | None = None) -> Config:
    path = find(path)
    if not path.exists():
        raise SystemExit(f"{path} not found; copy config.example.toml to {path} and edit it")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    base = path.resolve().parent
    return Config(
        library_path=_resolve(raw["library_path"], base),
        db_path=_resolve(raw.get("db_path", "data/music.db"), base),
    )


def _resolve(value: str, base: Path) -> Path:
    # Relative paths follow the config file, not the folder the command was run from.
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path
