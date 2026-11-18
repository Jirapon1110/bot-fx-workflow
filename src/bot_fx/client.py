"""HTTP client for the BOT Stat-ReferenceRate DAILY_REF_RATE endpoint."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import httpx

ENDPOINT = "https://gateway.api.bot.or.th/Stat-ReferenceRate/v2/DAILY_REF_RATE/"
TIMEOUT_SECONDS = 30.0


class ApiError(RuntimeError):
    """Raised when the API call fails or returns an unexpected payload."""


@dataclass(frozen=True)
class DailyRate:
    period: date
    rate: float  # Baht per 1 USD


def parse_response(payload: object) -> list[DailyRate]:
    """Extract rates from the API payload, sorted oldest -> newest, one per period.

    Rows with an empty rate are skipped; any other malformed row raises ApiError.
    """
    try:
        rows = payload["result"]["data"]["data_detail"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise ApiError(f"Unexpected response shape: {exc!r}") from exc
    if not isinstance(rows, list):
        raise ApiError("Unexpected response shape: data_detail is not a list")

    by_period: dict[date, DailyRate] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ApiError(f"Malformed data row {row!r}")
        raw = row.get("rate")
        if raw in (None, ""):
            continue
        try:
            rate = float(raw)
            period = date.fromisoformat(row["period"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ApiError(f"Malformed data row {row!r}: {exc}") from exc
        if not math.isfinite(rate):
            raise ApiError(f"Non-finite rate in row {row!r}")
        by_period[period] = DailyRate(period, rate)
    return sorted(by_period.values(), key=lambda r: r.period)


def fetch_daily_rates(
    token: str,
    start: date,
    end: date,
    client: httpx.Client | None = None,
) -> list[DailyRate]:
    """Fetch THB/USD rates for [start, end]. Pass `client` to inject a transport in tests."""
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        response = client.get(
            ENDPOINT,
            params={"start_period": start.isoformat(), "end_period": end.isoformat()},
            headers={"Authorization": token, "accept": "application/json"},
        )
        response.raise_for_status()
        return parse_response(response.json())
    except httpx.HTTPStatusError as exc:
        # Do not echo the request (it carries the token); status code is enough.
        raise ApiError(f"BOT API returned HTTP {exc.response.status_code}") from None
    except httpx.HTTPError as exc:
        raise ApiError(f"BOT API request failed: {type(exc).__name__}") from None
    except ValueError as exc:  # invalid JSON
        raise ApiError("BOT API returned invalid JSON") from exc
    finally:
        if owns_client:
            client.close()
