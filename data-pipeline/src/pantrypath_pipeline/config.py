"""Settings from config/pipeline.yaml, secrets from the repo-root .env.

PANTRYPATH_HOME points at the data-pipeline folder (set in Docker); by default it's
the folder this package lives in. PANTRYPATH_CONFIG and PANTRYPATH_ENV_FILE override
the config and .env locations.
"""

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_HOME = Path(__file__).resolve().parents[2]


class Config:
    """Parsed pipeline.yaml plus the folder its relative paths are resolved against."""

    def __init__(self, data: dict[str, Any], home: Path):
        self.data = data
        self.home = Path(home)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def path(self, rel: str | Path) -> Path:
        """Resolve a config path against the pipeline home (absolute paths pass through)."""
        p = Path(rel)
        return p if p.is_absolute() else self.home / p


def pipeline_home() -> Path:
    return Path(os.environ.get("PANTRYPATH_HOME") or DEFAULT_HOME)


def load_env(home: Path | None = None) -> None:
    """Load the repo-root .env without overriding variables that are already set."""
    env_file = os.environ.get("PANTRYPATH_ENV_FILE") or (home or pipeline_home()).parent / ".env"
    if Path(env_file).is_file():
        load_dotenv(env_file, override=False)


def load_config(path: str | Path | None = None, home: str | Path | None = None) -> Config:
    home = Path(home) if home else pipeline_home()
    load_env(home)
    default = home / "config" / "pipeline.yaml"
    cfg_path = Path(path or os.environ.get("PANTRYPATH_CONFIG") or default)
    with open(cfg_path) as f:
        data = yaml.safe_load(f)
    return Config(data, home)


def secret(name: str) -> str | None:
    """An environment variable's value, or None when unset or blank. Never log the value."""
    value = os.environ.get(name, "").strip()
    return value or None
