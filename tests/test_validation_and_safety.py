"""Validation edge cases and secret-safety guarantees (error messages, URLs,
tracebacks, and the token-traceback hardening in client.py)."""

import logging
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from bot_fx import cli, client as client_mod, workflow
from bot_fx.client import ApiError, ENDPOINT, DailyRate, fetch_daily_rates, parse_response
from bot_fx.config import ConfigError, Settings, load_settings
from bot_fx.storage import append_rates
from bot_fx.workflow import RunResult, run


def _p(rows):
    return {"result": {"data": {"data_detail": rows}}}


def _mock(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(rows=()):
    return _mock(lambda r: httpx.Response(200, json=_p(list(rows))))


def _settings(tmp_path):
    return Settings(token="tok", applicant_name="T", db_path=tmp_path / "x.db")


def _env(monkeypatch, tmp_path, name="Test", token="tok"):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_API_TOKEN", token)
    monkeypatch.setenv("APPLICANT_NAME", name)
    monkeypatch.delenv("BOT_DB_DIR", raising=False)


# ---- config.py ----
def test_gap_max_windows_is_exactly_four_hardcoded(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.params["start_period"])
        return httpx.Response(200, json=_p([]))

    with pytest.raises(ApiError):
        run(_settings(tmp_path), today=date(2026, 11, 19), client=_mock(handler))
    assert len(calls) == 4  # NOTES.md / CLAUDE.md promise "at most 4 windows"


def test_gap_name_may_contain_dash_and_underscore(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, name="Som-chai_01")
    assert load_settings().db_path.name == "bot_api_daily_Som-chai_01.db"


def test_gap_missing_token_message(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, token="")
    with pytest.raises(ConfigError, match="BOT_API_TOKEN is not set"):
        load_settings()


def test_gap_bad_name_message(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, name="a b")
    with pytest.raises(ConfigError, match="must be set and contain only letters, digits, '_' or '-'"):
        load_settings()


# ---- client.py ----
def test_gap_rate_just_below_limit_is_accepted():
    (r,) = parse_response(_p([{"period": "2026-01-02", "rate": "999.99"}]))
    assert r.rate == 999.99


def test_gap_request_url_and_accept_header():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url.copy_with(query=None))
        seen["accept"] = request.headers["accept"]
        return httpx.Response(200, json=_p([]))

    fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2), _mock(handler))
    assert seen["url"] == "https://gateway.api.bot.or.th/Stat-ReferenceRate/v2/DAILY_REF_RATE/"
    assert seen["accept"] == "application/json"
    assert ENDPOINT == seen["url"]


def test_gap_query_params_are_start_then_end():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json=_p([]))

    fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2), _mock(handler))
    assert seen == {"start_period": "2026-01-01", "end_period": "2026-01-02"}


@pytest.mark.parametrize("rate", [".5", "33."])
def test_gap_rate_regex_requires_digits_on_both_sides_of_the_dot(rate):
    with pytest.raises(ApiError):
        parse_response(_p([{"period": "2026-01-02", "rate": rate}]))


def test_gap_error_messages_name_the_problem():
    with pytest.raises(ApiError, match="bad period"):
        parse_response(_p([{"period": "x", "rate": "33.5"}]))
    with pytest.raises(ApiError, match="implausible rate"):
        parse_response(_p([{"period": "2026-01-02", "rate": "5000"}]))
    with pytest.raises(ApiError, match="Unexpected response shape: "):
        parse_response({"nope": 1})
    with pytest.raises(ApiError, match="data_detail is not a list"):
        parse_response(_p("x"))
    with pytest.raises(ApiError, match="Malformed data row"):
        parse_response(_p(["x"]))


def test_gap_integer_rate_is_accepted():
    (r,) = parse_response(_p([{"period": "2026-01-02", "rate": 33}]))
    assert r.rate == 33.0 and isinstance(r.rate, float)


def test_gap_huge_integer_error_message_is_truncated():
    with pytest.raises(ApiError) as exc:
        parse_response(_p([{"period": "2026-01-02", "rate": 10**400}]))
    # both the inner ValueError and the outer ApiError (which embeds the row repr
    # too) must stay short, so a hostile huge value can't bloat error text/logs.
    assert len(str(exc.value.__cause__)) < 100
    assert len(str(exc.value)) < 300


@pytest.mark.parametrize("rows", ["", {}, 5, 0])
def test_gap_non_list_data_detail_always_raises_apierror(rows):
    with pytest.raises(ApiError):
        parse_response(_p(rows))


@pytest.mark.parametrize("rate", [0, 0.0])
def test_gap_numeric_zero_rate_is_rejected_not_skipped(rate):
    with pytest.raises(ApiError):
        parse_response(_p([{"period": "2026-01-02", "rate": rate}]))


def test_gap_empty_rate_row_does_not_stop_parsing_later_rows():
    rows = [{"period": "2026-01-03", "rate": ""}, {"period": "2026-01-02", "rate": "33.5"}]
    assert [r.period for r in parse_response(_p(rows))] == [date(2026, 1, 2)]


def test_gap_row_error_is_chained_to_the_validation_error():
    with pytest.raises(ApiError) as exc:
        parse_response(_p([{"period": "2026-01-02", "rate": "abc"}]))
    assert isinstance(exc.value.__cause__, ValueError)


def test_gap_token_not_reachable_via_traceback_frame_locals():
    """A crash reporter (Sentry-style) that walks exc.__traceback__ and dumps every
    frame's f_locals must not recover the token, even though __cause__/__context__
    are already None (that only stops exc-chain tools, not frame-walking ones)."""
    secret = "REAL-SECRET-TOKEN-ABC123"
    client = _mock(lambda r: httpx.Response(500))
    with pytest.raises(ApiError) as exc:
        fetch_daily_rates(secret, date(2026, 1, 1), date(2026, 1, 2), client)

    tb = exc.value.__traceback__
    checked = []
    while tb is not None:
        # Only the library's own frames: the test's frame also binds `secret` as a
        # local (to pass it in), which is not the leak this test is about.
        if tb.tb_frame.f_globals.get("__name__") == "bot_fx.client":
            checked.append(tb.tb_frame.f_code.co_name)
            for value in tb.tb_frame.f_locals.values():
                assert value != secret
                if isinstance(value, httpx.Response):
                    assert value.request.headers.get("authorization") != secret
        tb = tb.tb_next
    assert "fetch_daily_rates" in checked  # sanity: we actually inspected that frame


def test_gap_unanticipated_exception_type_still_returns_normally_token_safe():
    """A non-HTTPError exception from the transport (e.g. a plain bug, or an httpx
    internal error like StreamError that doesn't subclass HTTPError) must still be
    converted to a (None, failure) return, not propagate with `token` live in
    _attempt_request's frame."""
    secret = "REAL-SECRET-TOKEN-XYZ789"

    def handler(request):
        raise RuntimeError("something unrelated to HTTP broke")

    with pytest.raises(ApiError, match="unexpectedly") as exc:
        fetch_daily_rates(secret, date(2026, 1, 1), date(2026, 1, 2), _mock(handler))

    tb = exc.value.__traceback__
    while tb is not None:
        if tb.tb_frame.f_globals.get("__name__") == "bot_fx.client":
            for value in tb.tb_frame.f_locals.values():
                assert value != secret
        tb = tb.tb_next


def test_gap_storage_failure_does_not_leak_token_via_run_frame(monkeypatch, tmp_path):
    """If append_rates raises (disk full, permissions, locked file - all OSError or
    sqlite3.Error, which cli.py explicitly handles), workflow.run's own frame must
    not still hold `settings` (and so the token) by that point."""
    secret = "REAL-SECRET-TOKEN-STORAGE"
    settings = Settings(token=secret, applicant_name="T", db_path=tmp_path / "x.db")
    rows = [{"period": f"2026-10-{d:02d}", "rate": "33.5"} for d in range(1, 21)]

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(workflow, "append_rates", boom)

    with pytest.raises(OSError) as exc:
        run(settings, today=date(2026, 10, 31), client=_ok(rows))
    tb = exc.value.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_name == "run" and tb.tb_frame.f_globals.get("__name__") == "bot_fx.workflow":
            assert "settings" not in tb.tb_frame.f_locals
        tb = tb.tb_next


def test_gap_timeout_is_wrapped_as_apierror():
    def handler(request):
        raise httpx.ReadTimeout("slow SECRET", request=request)

    with pytest.raises(ApiError, match="ReadTimeout") as exc:
        fetch_daily_rates("SECRET", date(2026, 1, 1), date(2026, 1, 2), _mock(handler))
    assert "SECRET" not in str(exc.value)


def test_gap_undecodable_body_is_reported_as_invalid_json_with_cause():
    c = _mock(lambda r: httpx.Response(200, content=b'{"a": "\xff"}'))
    with pytest.raises(ApiError, match="invalid JSON"):
        fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2), c)


def test_gap_invalid_json_message_and_cause():
    c = _mock(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(ApiError, match="BOT API returned invalid JSON"):
        fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2), c)


def test_gap_owned_client_is_closed_even_when_the_request_fails(monkeypatch):
    seen = {}
    real = httpx.Client

    def fake(**kw):
        seen["c"] = real(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        return seen["c"]

    monkeypatch.setattr(client_mod.httpx, "Client", fake)
    with pytest.raises(ApiError):
        fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2))
    assert seen["c"].is_closed


# ---- storage.py ----
def test_gap_fetched_at_is_not_null(tmp_path):
    db = tmp_path / "x.db"
    append_rates(db, [DailyRate(date(2026, 1, 2), 33.0)])
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO daily_ref_rate(period, rate, currency_pair, fetched_at) "
                "VALUES ('2026-01-03', 1, 'THB/USD', NULL)"
            )


# ---- workflow.py ----
def test_gap_default_today_uses_bangkok_not_tokyo(monkeypatch, tmp_path):
    fixed = datetime(2026, 11, 23, 16, 30, tzinfo=UTC)  # 23:30 on the 23rd in Bangkok, 01:30 on the 24th in Tokyo

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(workflow, "datetime", FakeDT)
    ends = []

    def handler(request):
        ends.append(request.url.params["end_period"])
        return httpx.Response(200, json=_p([]))

    with pytest.raises(ApiError):
        run(_settings(tmp_path), client=_mock(handler))
    assert ends[0] == "2026-11-23"


def test_gap_no_data_message(tmp_path):
    with pytest.raises(ApiError, match="no data points"):
        run(_settings(tmp_path), today=date(2026, 11, 1), client=_ok())


def test_gap_warns_when_exactly_19_points(tmp_path, caplog):
    rows = [{"period": f"2026-10-{d:02d}", "rate": "33.5"} for d in range(1, 20)]
    with caplog.at_level(logging.WARNING):
        r = run(_settings(tmp_path), today=date(2026, 11, 1), client=_ok(rows))
    assert r.fetched == 19
    assert "Only 19 data points available (wanted 20)" in caplog.text


# ---- cli.py ----
def test_gap_cli_configures_info_level_logging(monkeypatch, tmp_path):
    monkeypatch.setattr(logging.root, "handlers", [])
    monkeypatch.setattr(logging.root, "level", logging.WARNING)
    monkeypatch.delenv("BOT_API_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    cli.main()
    assert logging.root.level == logging.INFO


def test_gap_cli_does_not_swallow_unexpected_exceptions(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise ValueError("programming bug")

    monkeypatch.setattr(cli, "run", boom)
    with pytest.raises(ValueError, match="programming bug"):
        cli.main()


def test_gap_cli_error_format(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise ApiError("down")

    monkeypatch.setattr(cli, "run", boom)
    assert cli.main() == 1
    cap = capsys.readouterr()
    assert cap.err == "error: down\n" and cap.out == ""


def test_gap_cli_success_summary_format(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    result = RunResult(fetched=20, inserted=7, first=date(2026, 10, 1), last=date(2026, 10, 28))
    monkeypatch.setattr(cli, "run", lambda settings: result)
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert out.startswith("Fetched 20 points (2026-10-01 .. 2026-10-28); inserted 7 new rows into ")
    assert out.strip().endswith(str((tmp_path / ".venv" / "bot_api_daily_Test.db").resolve()))
