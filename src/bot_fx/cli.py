"""Command line entry point: `uv run bot-fx`."""

from __future__ import annotations

import logging
import sqlite3
import sys

from .client import ApiError
from .config import ConfigError, load_settings
from .workflow import run


def _make_output_safe() -> None:
    # With stdout/stderr redirected on Windows the encoding is often cp1252; a Thai
    # folder name in the printed path would then raise *after* the data was saved.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def main() -> int:
    _make_output_safe()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep request URLs out of logs
    try:
        settings = load_settings()
        result = run(settings)
    except (ConfigError, ApiError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    print(
        f"Fetched {result.fetched} points ({result.first} .. {result.last}); "
        f"inserted {result.inserted} new rows into {settings.db_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
