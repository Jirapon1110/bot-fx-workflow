"""Edge cases for date/rate parsing, API shape handling, window walk-back, and storage."""

import logging
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from bot_fx import cli, client as client_mod, workflow
from bot_fx.client import ApiError, DailyRate, fetch_daily_rates, parse_response
from bot_fx.config import MAX_WINDOWS, ConfigError, Settings, load_settings
from bot_fx.storage import append_rates
from bot_fx.workflow import run


def _settings(tmp_path):
    return Settings(token="tok", applicant_name="T", db_path=tmp_path / "x.db")


def _recording_client(days):
    calls = []

    def handler(request):
        s = date.fromisoformat(request.url.params["start_period"])
        e = date.fromisoformat(request.url.params["end_period"])
        calls.append((s, e))
        rows = [{"period": d.isoformat(), "rate": "33.5"} for d in sorted(days, reverse=True) if s <= d <= e]
        return httpx.Response(200, json={"result": {"data": {"data_detail": rows}}})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    c.calls = calls
    return c


# ---- client.py ----
def test_parse_duplicate_period_last_occurrence_wins():
    rows = [{"period": "2026-01-02", "rate": "33.1"}, {"period": "2026-01-02", "rate": "34.0"}]
    (r,) = parse_response({"result": {"data": {"data_detail": rows}}})
    assert r.rate == 34.0


@pytest.mark.parametrize("payload", [None, [], {"result": None}, {"result": {"data": None}}, "x"])
def test_parse_non_dict_payloads_raise_apierror(payload):
    with pytest.raises(ApiError):
        parse_response(payload)


def test_shape_error_keeps_its_cause():
    with pytest.raises(ApiError) as exc:
        parse_response({"nope": 1})
    assert isinstance(exc.value.__cause__, KeyError)


def test_connection_error_hides_token_cause_and_exception_text():
    def handler(request):
        raise httpx.ConnectError("boom SECRET-TOKEN-123", request=request)

    c = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as exc:
        fetch_daily_rates("SECRET-TOKEN-123", date(2026, 1, 1), date(2026, 1, 2), c)
    assert str(exc.value) == "BOT API request failed: ConnectError"
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_owned_client_has_timeout_and_is_closed(monkeypatch):
    seen = {}
    real = httpx.Client

    def fake(**kw):
        seen["kw"] = kw
        seen["c"] = real(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"result": {"data": {"data_detail": []}}})))
        return seen["c"]

    monkeypatch.setattr(client_mod.httpx, "Client", fake)
    fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2))
    assert seen["kw"]["timeout"] == 30.0
    assert seen["c"].is_closed


def test_injected_client_is_not_closed():
    c = _recording_client([])
    fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2), c)
    assert not c.is_closed


# ---- workflow.py ----
def test_window_is_exactly_30_calendar_days_and_contiguous():
    today = date(2026, 11, 19)
    c = _recording_client([])
    with pytest.raises(ApiError):
        run(Settings("tok", "T", Path("x.db")), today=today, client=c)
    assert len(c.calls) == MAX_WINDOWS
    assert c.calls[0] == (today - timedelta(days=29), today)
    for (s1, _), (_, e2) in zip(c.calls, c.calls[1:]):
        assert e2 == s1 - timedelta(days=1)  # no gap, no overlap


def test_exactly_20_in_first_window_stops_after_one_request(tmp_path):
    today = date(2026, 11, 19)
    c = _recording_client([today - timedelta(days=i) for i in range(20)])
    assert run(_settings(tmp_path), today=today, client=c).fetched == 20
    assert len(c.calls) == 1


def test_19_in_first_window_pulls_the_20th_from_next(tmp_path):
    today = date(2026, 11, 19)
    days = [today - timedelta(days=i) for i in range(19)] + [today - timedelta(days=40)]
    c = _recording_client(days)
    assert run(_settings(tmp_path), today=today, client=c).fetched == 20
    assert len(c.calls) == 2


def test_stops_after_max_windows(tmp_path):
    today = date(2026, 11, 19)
    far = today - timedelta(days=30 * MAX_WINDOWS + 1)  # just outside the last window
    c = _recording_client([far])
    with pytest.raises(ApiError):
        run(_settings(tmp_path), today=today, client=c)
    inside = today - timedelta(days=30 * MAX_WINDOWS - 1)  # last day of the last window
    c = _recording_client([inside])
    assert run(_settings(tmp_path), today=today, client=c).fetched == 1


def test_multi_window_result_is_the_newest_20_in_order(tmp_path):
    today = date(2026, 11, 19)
    new = [today - timedelta(days=i) for i in range(10)]
    old = [today - timedelta(days=35 + i) for i in range(15)]
    s = _settings(tmp_path)
    r = run(s, today=today, client=_recording_client(new + old))
    assert r.last == today and r.first == today - timedelta(days=35 + 9)
    with sqlite3.connect(s.db_path) as conn:
        got = [p for (p,) in conn.execute("SELECT period FROM daily_ref_rate ORDER BY period")]
    assert got == sorted(d.isoformat() for d in new + old[:10])


def test_collect_dedupes_a_period_returned_by_two_windows(monkeypatch):
    monkeypatch.setattr(workflow, "fetch_daily_rates", lambda *a: [DailyRate(date(2026, 1, 2), 33.0)])
    assert len(workflow._collect("tok", date(2026, 1, 31), None)) == 1


def test_short_data_logs_a_warning(tmp_path, caplog):
    c = _recording_client([date(2026, 11, 1)])
    with caplog.at_level(logging.WARNING):
        run(_settings(tmp_path), today=date(2026, 11, 1), client=c)
    assert "Only 1 data points" in caplog.text


def test_default_today_is_the_bangkok_date(monkeypatch, tmp_path):
    fixed = datetime(2026, 11, 23, 17, 30, tzinfo=UTC)  # 00:30 on the 24th in Bangkok

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(workflow, "datetime", FakeDT)
    c = _recording_client([date(2026, 11, 24)])
    run(_settings(tmp_path), client=c)
    assert c.calls[0][1] == date(2026, 11, 24)


# ---- config.py ----
def _env(monkeypatch, tmp_path, token="tok", name="Test"):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_API_TOKEN", token)
    monkeypatch.setenv("APPLICANT_NAME", name)
    monkeypatch.delenv("BOT_DB_DIR", raising=False)


@pytest.mark.parametrize("bad", ["a\tb", "a\nb", "tok\x00", "tok\x7f"])
def test_token_with_control_chars_rejected(monkeypatch, tmp_path, bad):
    _env(monkeypatch, tmp_path, token=bad)
    with pytest.raises(ConfigError):
        load_settings()


def test_token_and_name_are_stripped(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, token="  tok \n", name="  Test ")
    s = load_settings()
    assert s.token == "tok" and s.db_path.name == "bot_api_daily_Test.db"


@pytest.mark.parametrize("name", ["", "   ", "a.b", "a b", "a/b", "a\\b", "Test/..","\u00e9"])
def test_applicant_name_must_be_strict(monkeypatch, tmp_path, name):
    _env(monkeypatch, tmp_path, name=name)
    with pytest.raises(ConfigError):
        load_settings()


def test_relative_paths_anchor_on_dotenv_dir_not_cwd(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("")
    sub = tmp_path / "sub"
    sub.mkdir()
    _env(monkeypatch, sub)
    assert load_settings().db_path.parent == (tmp_path / ".venv").resolve()


def test_db_path_is_normalised(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("BOT_DB_DIR", "a/../b")
    p = load_settings().db_path
    assert ".." not in p.parts and p.parent == (tmp_path / "b").resolve()


# ---- storage.py ----
def test_storage_rejects_zero_rate(tmp_path):
    with pytest.raises(sqlite3.IntegrityError):
        append_rates(tmp_path / "x.db", [DailyRate(date(2026, 1, 2), 0.0)])


def test_storage_schema_and_row_contents(tmp_path):
    db = tmp_path / "x.db"
    append_rates(db, [DailyRate(date(2026, 1, 2), 33.25)])
    with sqlite3.connect(db) as conn:
        period, rate, pair, fetched = conn.execute("SELECT * FROM daily_ref_rate").fetchone()
        with pytest.raises(sqlite3.IntegrityError):  # currency_pair must be NOT NULL
            conn.execute("INSERT INTO daily_ref_rate(period, rate, fetched_at) VALUES ('2026-01-03', 1, 't')")
    assert (period, rate, pair) == ("2026-01-02", 33.25, "THB/USD")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", fetched)  # UTC, seconds


def test_storage_creates_missing_parent_dirs(tmp_path):
    assert append_rates(tmp_path / "a" / "b" / "x.db", [DailyRate(date(2026, 1, 2), 33.0)]) == 1


def test_storage_closes_connection(monkeypatch, tmp_path):
    closed = []

    class Tracking(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda p: real(p, factory=Tracking))
    append_rates(tmp_path / "x.db", [DailyRate(date(2026, 1, 2), 33.0)])
    assert closed == [True]


# ---- cli.py ----
def test_cli_returns_1_on_os_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_API_TOKEN", "tok")
    monkeypatch.setenv("APPLICANT_NAME", "Test")

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(cli, "run", boom)
    assert cli.main() == 1


def test_cli_silences_httpx_logger(monkeypatch, tmp_path):
    lg = logging.getLogger("httpx")
    monkeypatch.setattr(lg, "level", logging.NOTSET)
    monkeypatch.delenv("BOT_API_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    cli.main()
    assert lg.level == logging.WARNING
