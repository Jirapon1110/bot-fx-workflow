"""Settings loaded from environment variables (optionally from a .env file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, find_dotenv

# How many daily data points to keep (the task asks for 20).
N_POINTS = 20
# The API answers HTTP 400 for ranges of ~35+ days (31 days verified OK), so each
# request spans at most WINDOW_DAYS calendar days; MAX_WINDOWS bounds the walk back.
WINDOW_DAYS = 30
MAX_WINDOWS = 4

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

# src/bot_fx/config.py -> project root. Module-level so tests can point it elsewhere.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)  # keep the secret out of repr(), logs and tracebacks
    applicant_name: str
    db_path: Path  # absolute


def _find_env_file() -> str:
    """Locate .env: the project's own first, otherwise search upward from the cwd.

    Looking at the project first makes the CLI work when started from elsewhere
    (Task Scheduler, a shortcut, `uv run --project`). The pyproject.toml check keeps
    this from matching an unrelated folder if the package is installed non-editable.
    """
    project_env = PROJECT_ROOT / ".env"
    if (PROJECT_ROOT / "pyproject.toml").is_file() and project_env.is_file():
        return str(project_env)
    return find_dotenv(usecwd=True)


def load_settings() -> Settings:
    dotenv_path = _find_env_file()
    try:
        file_values = dotenv_values(dotenv_path, encoding="utf-8") if dotenv_path else {}
    except UnicodeDecodeError:
        raise ConfigError(
            ".env could not be read as UTF-8 (was it saved as UTF-16 'Unicode'? "
            "re-save it as UTF-8)."
        ) from None
    # Anchor relative paths on the project (where .env lives), not on wherever the
    # command happens to be run from.
    root = Path(dotenv_path).parent if dotenv_path else Path.cwd()

    def get(name: str) -> str:
        # A non-empty environment variable wins over .env; an empty one (e.g. a stray
        # `BOT_API_TOKEN=` in the shell) must not hide the value stored in .env.
        return (os.environ.get(name) or file_values.get(name) or "").strip()

    token = get("BOT_API_TOKEN")
    if not token:
        raise ConfigError("BOT_API_TOKEN is not set (put it in .env, never in git).")
    if not token.isascii() or not token.isprintable() or " " in token:
        raise ConfigError(
            "BOT_API_TOKEN must be plain printable ASCII without spaces "
            "(was it copied with extra text?)."
        )

    name = get("APPLICANT_NAME")
    if not _NAME_RE.fullmatch(name):
        raise ConfigError(
            "APPLICANT_NAME must be set and contain only letters, digits, '_' or '-' "
            "(it becomes part of the database file name)."
        )

    # The task asks for the database to live inside the project's .venv folder.
    db_dir = root / (get("BOT_DB_DIR") or ".venv")
    return Settings(
        token=token,
        applicant_name=name,
        db_path=(db_dir / f"bot_api_daily_{name}.db").resolve(),
    )
