"""Settings loaded from environment variables (optionally from a .env file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# How many daily data points to keep (the task asks for 20).
N_POINTS = 20
# The API answers HTTP 400 for ranges of ~35+ days (31 days verified OK), so each
# request spans at most WINDOW_DAYS calendar days; MAX_WINDOWS bounds the walk back.
WINDOW_DAYS = 30
MAX_WINDOWS = 4

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    token: str
    applicant_name: str
    db_path: Path  # absolute


def load_settings() -> Settings:
    dotenv_path = find_dotenv(usecwd=True)
    load_dotenv(dotenv_path)
    # Anchor relative paths on the project (where .env lives), not on wherever the
    # command happens to be run from.
    root = Path(dotenv_path).parent if dotenv_path else Path.cwd()

    token = os.environ.get("BOT_API_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_API_TOKEN is not set (put it in .env, never in git).")

    name = os.environ.get("APPLICANT_NAME", "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ConfigError(
            "APPLICANT_NAME must be set and contain only letters, digits, '_' or '-' "
            "(it becomes part of the database file name)."
        )

    # The task asks for the database to live inside the project's .venv folder.
    db_dir = root / os.environ.get("BOT_DB_DIR", ".venv")
    return Settings(
        token=token,
        applicant_name=name,
        db_path=(db_dir / f"bot_api_daily_{name}.db").resolve(),
    )
