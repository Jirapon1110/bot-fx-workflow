"""HTTP client for the BOT Stat-ReferenceRate DAILY_REF_RATE endpoint."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date

import httpx

ENDPOINT = "https://gateway.api.bot.or.th/Stat-ReferenceRate/v2/DAILY_REF_RATE/"
TIMEOUT_SECONDS = 30.0

# Sanity bound for Baht per 1 USD. Stored rows are never revised (append-only), so a
# garbage value must be rejected here rather than fixed later.
MAX_PLAUSIBLE_RATE = 1000.0

# A genuine response holds at most WINDOW_DAYS (30) rows; 100x that is already absurd.
# Guards against unbounded memory use from a misbehaving/compromised endpoint - this
# is not attacker-reachable under normal use, but costs nothing to check.
MAX_RESPONSE_BYTES = 2_000_000
MAX_ROWS = 3000

# [0-9], not \d: in Python \d also matches non-ASCII digits such as Arabic-Indic ones.
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_RATE_RE = re.compile(r"[0-9]+(\.[0-9]+)?")


class ApiError(RuntimeError):
    """Raised when the API call fails or returns an unexpected payload."""


def _short_repr(value: object, limit: int = 200) -> str:
    """repr() truncated so a hostile/huge value (e.g. a 400-digit rate) can't bloat
    error messages or logs."""
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass(frozen=True)
class DailyRate:
    period: date
    rate: float  # Baht per 1 USD


def _parse_period(raw: object) -> date:
    # Strict YYYY-MM-DD: date.fromisoformat also accepts "20261123" and "2026-W47-1".
    if not isinstance(raw, str) or not _DATE_RE.fullmatch(raw):
        raise ValueError(f"bad period {_short_repr(raw)}")
    return date.fromisoformat(raw)


def _parse_rate(raw: object) -> float:
    # float() alone accepts "1_0", "-5", "nan", True and non-ASCII digits.
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError(f"bad rate {_short_repr(raw)}")
    if isinstance(raw, str) and not _RATE_RE.fullmatch(raw):
        raise ValueError(f"bad rate {_short_repr(raw)}")
    try:
        rate = float(raw)
    except OverflowError:  # e.g. a JSON integer with 400 digits
        raise ValueError(f"implausible rate {_short_repr(raw, limit=40)}") from None
    if not math.isfinite(rate) or not 0 < rate < MAX_PLAUSIBLE_RATE:
        raise ValueError(f"implausible rate {_short_repr(raw, limit=40)}")
    return rate


def parse_response(
    payload: object, start: date | None = None, end: date | None = None
) -> list[DailyRate]:
    """Extract rates from the API payload, sorted oldest -> newest, one per period.

    Rows with an empty rate are skipped; any other malformed row raises ApiError.
    If a period is repeated in one response, the last occurrence wins. When `start`/`end`
    are given, rows whose (valid) period lies outside [start, end] are dropped *before*
    their rate is validated, so a bad row we never wanted cannot abort the run.
    """
    try:
        rows = payload["result"]["data"]["data_detail"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise ApiError(f"Unexpected response shape: {_short_repr(exc)}") from exc
    if not isinstance(rows, list):
        raise ApiError("Unexpected response shape: data_detail is not a list")
    if len(rows) > MAX_ROWS:
        raise ApiError(f"data_detail has {len(rows)} rows, more than the {MAX_ROWS} sanity cap")

    by_period: dict[date, DailyRate] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ApiError(f"Malformed data row {_short_repr(row)}")
        raw = row.get("rate")
        if raw in (None, ""):
            continue
        try:
            period = _parse_period(row.get("period"))
            if (start and period < start) or (end and period > end):
                continue
            rate = _parse_rate(raw)
        except ValueError as exc:
            raise ApiError(f"Malformed data row {_short_repr(row)}: {exc}") from exc
        by_period[period] = DailyRate(period, rate)
    return sorted(by_period.values(), key=lambda r: r.period)


def _attempt_request(client: httpx.Client, token: str, start: date, end: date) -> tuple[object, str | None]:
    """Make the GET and return (payload, failure) - never raises.

    Kept as its own frame, separate from fetch_daily_rates: this function returns
    normally in every case (success or failure), so once it returns, nothing keeps
    this frame (and the `token`/`headers`/`response` locals it held) alive. If an
    ApiError is later raised in the caller, its traceback therefore cannot reach the
    plaintext token through this frame's locals, the way it could if the token were
    still a local in the very frame that raises.
    """
    try:
        response = client.get(
            ENDPOINT,
            params={"start_period": start.isoformat(), "end_period": end.isoformat()},
            headers={"Authorization": token, "accept": "application/json"},
        )
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            return None, f"BOT API response exceeds the {MAX_RESPONSE_BYTES}-byte sanity cap"
        return response.json(), None
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        hint = (
            " (check BOT_API_TOKEN and that the app is subscribed to Exchange Rates)"
            if status in (401, 403)
            else ""
        )
        return None, f"BOT API returned HTTP {status}{hint}"
    except httpx.HTTPError as exc:
        return None, f"BOT API request failed: {type(exc).__name__}"
    except UnicodeEncodeError:
        return None, "BOT_API_TOKEN contains characters not allowed in an HTTP header"
    except ValueError:  # invalid JSON
        return None, "BOT API returned invalid JSON"
    except Exception:
        # Anything else (e.g. an httpx internal error that isn't an HTTPError
        # subclass, such as StreamError/InvalidURL) must still return normally:
        # that is the entire basis for this frame being traceback-safe. Without
        # this clause, an unanticipated exception type would propagate with
        # `token` still bound in this frame's locals.
        return None, "BOT API request failed unexpectedly"


def fetch_daily_rates(
    token: str,
    start: date,
    end: date,
    client: httpx.Client | None = None,
) -> list[DailyRate]:
    """Fetch THB/USD rates for [start, end]. Pass `client` to inject a transport in tests.

    Rows outside [start, end] are dropped: a misbehaving API must not be able to
    insert (permanently, since storage is append-only) a future-dated or ancient row.
    """
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        payload, failure = _attempt_request(client, token, start, end)
    finally:
        # `token` is no longer needed; drop it from this frame's locals before any
        # possible raise below, so it can never surface via exc.__traceback__ either
        # (see _attempt_request's docstring for why that frame is already safe).
        del token
        if owns_client:
            client.close()
    if failure is not None:
        raise ApiError(failure)
    return parse_response(payload, start, end)
