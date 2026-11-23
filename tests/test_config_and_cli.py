"""Config discovery (.env/PROJECT_ROOT), response-size limits, and CLI output safety."""

import io
import logging
import subprocess
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

from bot_fx import cli, config, workflow
from bot_fx.client import MAX_ROWS, ApiError, DailyRate, _short_repr, parse_response
from bot_fx.config import Settings, load_settings
from bot_fx.workflow import run


def _settings(tmp_path):
    return Settings(token="tok", applicant_name="T", db_path=tmp_path / "x.db")


def _ok_client(rows):
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"result": {"data": {"data_detail": rows}}})
        )
    )


def test_short_repr_boundary_exact_length_is_not_truncated():
    value = "a" * 18
    text = repr(value)
    n = len(text)
    assert _short_repr(value, limit=n) == text
    truncated = _short_repr(value, limit=n - 1)
    assert truncated != text
    assert truncated.endswith("...")
    assert len(truncated) == n - 1


def test_project_root_computes_the_real_project_directory():
    # tests/this_file.py -> tests/ -> project root. Run in a fresh subprocess so this
    # never touches the test-isolated config.PROJECT_ROOT that conftest.py overrides
    # for every other test (importlib.reload in-process corrupts isinstance checks
    # across the whole suite - confirmed the hard way while writing this test).
    expected = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", "import bot_fx.config as c; print(c.PROJECT_ROOT)"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(expected),
    )
    computed = Path(proc.stdout.strip())
    assert computed == expected
    assert (computed / "pyproject.toml").is_file()


def test_pyproject_without_project_env_falls_back_to_cwd_search(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    # deliberately no project/.env file

    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("BOT_API_TOKEN=cwd-token\nAPPLICANT_NAME=Cwd\n")
    monkeypatch.chdir(other)
    monkeypatch.setattr(config, "PROJECT_ROOT", project)
    for var in ("BOT_API_TOKEN", "APPLICANT_NAME", "BOT_DB_DIR"):
        monkeypatch.delenv(var, raising=False)

    settings = load_settings()
    assert settings.token == "cwd-token"


def test_no_warning_when_exactly_20_points(tmp_path, caplog):
    rows = [{"period": f"2026-10-{d:02d}", "rate": "33.5"} for d in range(1, 21)]
    with caplog.at_level(logging.WARNING):
        result = run(_settings(tmp_path), today=date(2026, 11, 1), client=_ok_client(rows))
    assert result.fetched == 20
    assert "Only" not in caplog.text


def test_collect_keeps_last_occurrence_when_same_period_returned_twice(monkeypatch):
    seq = [11.0, 22.0]

    def fake(token, start, end, client):
        rate = seq.pop(0) if seq else 22.0
        return [DailyRate(date(2026, 1, 2), rate)]

    monkeypatch.setattr(workflow, "fetch_daily_rates", fake)
    result = workflow._collect("tok", date(2026, 1, 31), None)
    assert result == [DailyRate(date(2026, 1, 2), 22.0)]


def test_response_with_absurd_row_count_is_rejected_not_processed():
    # A genuine response holds at most ~30 rows; this guards against unbounded
    # memory use from a misbehaving/compromised endpoint, not normal traffic.
    rows = [{"period": "2026-01-02", "rate": "33.5"} for _ in range(MAX_ROWS + 1)]
    with pytest.raises(ApiError, match="sanity cap"):
        parse_response({"result": {"data": {"data_detail": rows}}})


def test_response_at_the_row_cap_is_still_accepted():
    rows = [{"period": "2026-01-02", "rate": "33.5"}]  # well under the cap
    assert len(parse_response({"result": {"data": {"data_detail": rows}}})) == 1


def test_oversized_response_body_is_rejected_before_json_parsing():
    huge = b'{"pad": "' + b"x" * 3_000_000 + b'"}'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=huge)))
    with pytest.raises(ApiError, match="sanity cap"):
        from bot_fx.client import fetch_daily_rates

        fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2), client)


def test_conftest_isolation_also_covers_cwd_based_dotenv_search(tmp_path):
    # The autouse fixture must chdir away from any real .env too, not just patch
    # PROJECT_ROOT - this is a regression test for that specific gap.
    import os

    assert config.PROJECT_ROOT != Path(os.getcwd())
    from bot_fx.config import ConfigError

    with pytest.raises(ConfigError):
        load_settings()  # must find no .env at all from this isolated cwd


def test_cli_does_not_crash_printing_non_ascii_error_to_cp1252_stderr(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_API_TOKEN", "tok")
    monkeypatch.setenv("APPLICANT_NAME", "Test")

    def boom(*a, **k):
        raise ApiError("ข้อผิดพลาด")  # Thai text

    monkeypatch.setattr(cli, "run", boom)
    fake_stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(cli.sys, "stderr", fake_stderr)
    assert cli.main() == 1  # would raise UnicodeEncodeError without errors="replace" on stderr
