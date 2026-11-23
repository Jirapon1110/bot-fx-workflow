"""Orchestration: API -> latest 20 data points -> SQLite."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .client import ApiError, DailyRate, fetch_daily_rates
from .config import MAX_WINDOWS, N_POINTS, WINDOW_DAYS, Settings
from .storage import append_rates

log = logging.getLogger(__name__)

BANGKOK = ZoneInfo("Asia/Bangkok")  # BOT publishes rates by Thai date


@dataclass(frozen=True)
class RunResult:
    fetched: int
    inserted: int
    first: date
    last: date


def _collect(token: str, end: date, client: httpx.Client | None) -> list[DailyRate]:
    """Walk back in WINDOW_DAYS-sized, non-overlapping windows until N_POINTS are found.

    The API rejects ranges longer than ~31 days (HTTP 400), and holidays mean a
    single window may hold fewer than N_POINTS business days.
    """
    by_period: dict[date, DailyRate] = {}
    for _ in range(MAX_WINDOWS):
        start = end - timedelta(days=WINDOW_DAYS - 1)
        for rate in fetch_daily_rates(token, start, end, client):
            by_period[rate.period] = rate  # de-dupe in case the API repeats a day
        if len(by_period) >= N_POINTS:
            break
        end = start - timedelta(days=1)
    return sorted(by_period.values(), key=lambda r: r.period)


def run(
    settings: Settings,
    today: date | None = None,
    client: httpx.Client | None = None,
) -> RunResult:
    end = today or datetime.now(BANGKOK).date()

    rates = _collect(settings.token, end, client)[-N_POINTS:]  # newest N_POINTS
    db_path = settings.db_path
    # The token is no longer needed from here on, and the next call (append_rates)
    # can raise sqlite3.Error/OSError (disk full, permissions, a locked file) - an
    # ordinary, expected failure mode that cli.py explicitly handles. `settings`
    # must not still be a local of this frame when that happens, or the token
    # becomes reachable via exc.__traceback__ through `settings.token`, the same
    # way client.py's `_attempt_request` closes off that path for the HTTP layer.
    del settings
    if not rates:
        raise ApiError("BOT API returned no data points")
    if len(rates) < N_POINTS:
        log.warning("Only %d data points available (wanted %d)", len(rates), N_POINTS)

    inserted = append_rates(db_path, rates)
    return RunResult(
        fetched=len(rates),
        inserted=inserted,
        first=rates[0].period,
        last=rates[-1].period,
    )


# Known, accepted limit to the above: cli.main() (and any other caller) still holds
# the `Settings` object as a local for the duration of this call, so if the
# exception propagates all the way up through the caller's own frame, that frame
# still exposes the token via `settings.token`. Closing that too would mean every
# frame in the chain catches its own exceptions internally and returns a plain
# value instead of letting anything propagate (the same restructuring applied to
# `_attempt_request`), which is disproportionate for this project; this module and
# the caller's frame are the two places a bug actually put the token at the
# narrowest, most proximate scope.
