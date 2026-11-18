import sqlite3
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from bot_fx import cli
from bot_fx.client import ApiError, DailyRate, fetch_daily_rates, parse_response
from bot_fx.config import ConfigError, Settings, load_settings
from bot_fx.storage import append_rates
from bot_fx.workflow import RunResult, run


def _payload(n: int, start: date = date(2026, 10, 1)) -> dict:
    rows = [
        {"period": (start + timedelta(days=i)).isoformat(), "rate": f"{33 + i / 100:.7f}"}
        for i in range(n)
    ]
    # The real API returns newest first.
    return {"result": {"data": {"data_detail": list(reversed(rows))}}}


def _client(payload: dict | None = None, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "tok"
        assert "start_period" in request.url.params and "end_period" in request.url.params
        return httpx.Response(status, json=payload or {})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _settings(tmp_path: Path) -> Settings:
    return Settings(token="tok", applicant_name="Test", db_path=tmp_path / "x.db")


def test_parse_sorts_oldest_first_and_skips_empty_rates():
    payload = _payload(3)
    payload["result"]["data"]["data_detail"].append({"period": "2026-09-01", "rate": ""})
    rates = parse_response(payload)
    assert [r.period for r in rates] == sorted(r.period for r in rates)
    assert len(rates) == 3
    assert isinstance(rates[0].rate, float)


def test_parse_rejects_unexpected_shape():
    with pytest.raises(ApiError):
        parse_response({"nope": 1})


def test_http_error_does_not_leak_token():
    secret = "SECRET-TOKEN-123"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as exc:
        fetch_daily_rates(secret, date(2026, 1, 1), date(2026, 1, 2), client)
    assert "401" in str(exc.value) and secret not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_connection_error_is_wrapped_without_token():
    secret = "SECRET-TOKEN-123"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as exc:
        fetch_daily_rates(secret, date(2026, 1, 1), date(2026, 1, 2), client)
    assert secret not in str(exc.value)


def test_invalid_json_is_wrapped():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>"))
    )
    with pytest.raises(ApiError):
        fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2), client)


@pytest.mark.parametrize(
    "rows",
    [
        "not-a-list",
        ["not-a-dict"],
        [{"period": "2026-01-02", "rate": "nan"}],
        [{"period": "2026-01-02", "rate": "inf"}],
        [{"period": "bad-date", "rate": "33.1"}],
        [{"rate": "33.1"}],
        [{"period": "2026-01-02", "rate": "abc"}],
    ],
)
def test_parse_rejects_malformed_rows(rows):
    with pytest.raises(ApiError):
        parse_response({"result": {"data": {"data_detail": rows}}})


def test_parse_dedupes_repeated_periods():
    row = {"period": "2026-01-02", "rate": "33.1"}
    rates = parse_response({"result": {"data": {"data_detail": [row, row]}}})
    assert len(rates) == 1


def test_run_keeps_newest_20_points(tmp_path):
    result = run(_settings(tmp_path), today=date(2026, 11, 1), client=_client(_payload(30)))
    assert result.fetched == 20
    assert result.last == date(2026, 10, 30)  # newest of the 30 generated days
    assert result.first == date(2026, 10, 11)


def test_run_stores_20_rows_and_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    client = _client(_payload(30))

    first = run(settings, today=date(2026, 11, 1), client=client)
    assert (first.fetched, first.inserted) == (20, 20)

    second = run(settings, today=date(2026, 11, 1), client=client)
    assert (second.fetched, second.inserted) == (20, 0)

    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_ref_rate").fetchone()[0] == 20


def test_run_appends_only_new_days(tmp_path):
    settings = _settings(tmp_path)
    run(settings, today=date(2026, 11, 1), client=_client(_payload(25)))
    result = run(settings, today=date(2026, 11, 2), client=_client(_payload(27)))
    assert result.inserted == 2


def _range_aware_client(business_days: list[date]) -> httpx.Client:
    """Mimics the real API: filters by range and rejects ranges over 34 days."""
    calls: list[tuple[date, date]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = date.fromisoformat(request.url.params["start_period"])
        end = date.fromisoformat(request.url.params["end_period"])
        calls.append((start, end))
        if (end - start).days > 34:
            return httpx.Response(400)
        rows = [
            {"period": d.isoformat(), "rate": "33.5"}
            for d in sorted(business_days, reverse=True)
            if start <= d <= end
        ]
        return httpx.Response(200, json={"result": {"data": {"data_detail": rows}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.calls = calls  # type: ignore[attr-defined]
    return client


def test_run_walks_back_in_windows_when_holidays_leave_too_few_points(tmp_path):
    today = date(2026, 11, 19)
    # Only 8 business days in the latest window, the rest ~40 days back.
    days = [today - timedelta(days=i) for i in range(0, 12, 1) if i % 3 != 2]
    days += [today - timedelta(days=i) for i in range(35, 60) if i % 7 not in (0, 1)]
    client = _range_aware_client(days)

    result = run(_settings(tmp_path), today=today, client=client)

    assert result.fetched == 20
    assert len(client.calls) >= 2
    assert all((e - s).days <= 30 for s, e in client.calls)
    assert client.calls[1][1] < client.calls[0][0]  # windows do not overlap


def test_run_raises_when_api_returns_no_data(tmp_path):
    empty = {"result": {"data": {"data_detail": []}}}
    settings = _settings(tmp_path)
    with pytest.raises(ApiError):
        run(settings, today=date(2026, 11, 1), client=_client(empty))
    assert not settings.db_path.exists()  # nothing created for an empty run


def test_run_warns_but_stores_when_fewer_than_20_points(tmp_path):
    result = run(_settings(tmp_path), today=date(2026, 11, 1), client=_client(_payload(5)))
    assert (result.fetched, result.inserted) == (5, 5)


def test_storage_rejects_null_rate_instead_of_silently_skipping(tmp_path):
    bad = DailyRate(date(2026, 1, 2), None)  # type: ignore[arg-type]
    with pytest.raises(sqlite3.IntegrityError):
        append_rates(tmp_path / "x.db", [bad])


def test_storage_does_not_overwrite_existing_period(tmp_path):
    db = tmp_path / "x.db"
    append_rates(db, [DailyRate(date(2026, 1, 2), 33.0)])
    assert append_rates(db, [DailyRate(date(2026, 1, 2), 34.0)]) == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT rate FROM daily_ref_rate").fetchone() == (33.0,)


def _cli_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_API_TOKEN", "tok")
    monkeypatch.setenv("APPLICANT_NAME", "Test")


def test_cli_returns_1_on_config_error(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_API_TOKEN", raising=False)
    assert cli.main() == 1
    assert "BOT_API_TOKEN" in capsys.readouterr().err


def test_cli_returns_1_on_api_error(monkeypatch, tmp_path, capsys):
    _cli_env(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise ApiError("down")

    monkeypatch.setattr(cli, "run", boom)
    assert cli.main() == 1
    assert "down" in capsys.readouterr().err


def test_cli_returns_1_on_storage_error(monkeypatch, tmp_path):
    _cli_env(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "run", boom)
    assert cli.main() == 1


def test_cli_success_prints_absolute_db_path(monkeypatch, tmp_path, capsys):
    _cli_env(monkeypatch, tmp_path)
    result = RunResult(fetched=20, inserted=20, first=date(2026, 10, 1), last=date(2026, 10, 28))
    monkeypatch.setattr(cli, "run", lambda settings: result)
    assert cli.main() == 0
    assert str(tmp_path.resolve()) in capsys.readouterr().out


def test_config_requires_token_and_safe_name(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("BOT_API_TOKEN", raising=False)
    monkeypatch.setenv("APPLICANT_NAME", "Somchai")
    with pytest.raises(ConfigError):
        load_settings()

    monkeypatch.setenv("BOT_API_TOKEN", "t")
    monkeypatch.setenv("APPLICANT_NAME", "../evil")
    with pytest.raises(ConfigError):
        load_settings()

    monkeypatch.setenv("APPLICANT_NAME", "Somchai")
    assert load_settings().db_path.name == "bot_api_daily_Somchai.db"
