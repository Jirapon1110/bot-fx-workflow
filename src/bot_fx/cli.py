"""Command line entry point: `uv run bot-fx`."""

from __future__ import annotations

import logging
import sqlite3
import sys

from .client import ApiError
from .config import ConfigError, load_settings
from .workflow import run


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep request URLs out of logs
    try:
        settings = load_settings()
        result = run(settings)
    except (ConfigError, ApiError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Fetched {result.fetched} points ({result.first} .. {result.last}); "
        f"inserted {result.inserted} new rows into {settings.db_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
