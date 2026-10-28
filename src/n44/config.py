"""Repository paths and options from config/base.yaml."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = REPO_ROOT / "config" / "base.yaml"


@cache
def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def repo_path(*parts: str | Path) -> Path:
    """Absolute path of a location given relative to the repository root."""
    return REPO_ROOT.joinpath(*parts)


def configured_path(key: str) -> Path:
    """Absolute path of an entry of the ``paths`` section of the configuration."""
    return repo_path(load_config()["paths"][key])
