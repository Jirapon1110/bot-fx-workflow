"""CLI robustness: .env encoding/location, non-ASCII output, interrupts, and errors."""

import io
from datetime import date
from pathlib import Path

import httpx
import pytest

from bot_fx import cli, config
from bot_fx.client import ApiError, fetch_daily_rates
from bot_fx.config import ConfigError, load_settings
from bot_fx.workflow import RunResult


def test_utf16_dotenv_gives_a_clear_config_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for var in ("BOT_API_TOKEN", "APPLICANT_NAME"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_bytes("BOT_API_TOKEN=abc\nAPPLICANT_NAME=X\n".encode("utf-16"))
    with pytest.raises(ConfigError, match="UTF-8"):
        load_settings()


def test_project_env_is_used_even_when_started_from_another_folder(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    (project / ".env").write_text("BOT_API_TOKEN=proj-token\nAPPLICANT_NAME=Proj\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    for var in ("BOT_API_TOKEN", "APPLICANT_NAME", "BOT_DB_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "PROJECT_ROOT", project)

    settings = load_settings()

    assert settings.token == "proj-token"
    assert settings.db_path == (project / ".venv" / "bot_api_daily_Proj.db").resolve()


def test_project_root_without_pyproject_is_ignored(monkeypatch, tmp_path):
    # e.g. the package was installed non-editable, so parents[2] is some unrelated folder
    (tmp_path / ".env").write_text("BOT_API_TOKEN=wrong\nAPPLICANT_NAME=Wrong\n")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    for var in ("BOT_API_TOKEN", "APPLICANT_NAME"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    # found via the normal upward cwd search, not via PROJECT_ROOT
    assert load_settings().token == "wrong"


def test_cli_keyboard_interrupt_exits_130(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_API_TOKEN", "tok")
    monkeypatch.setenv("APPLICANT_NAME", "Test")

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", boom)
    assert cli.main() == 130
    assert "interrupted" in capsys.readouterr().err


def test_cli_does_not_crash_printing_non_ascii_path_to_cp1252_stdout(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_API_TOKEN", "tok")
    monkeypatch.setenv("APPLICANT_NAME", "Test")
    monkeypatch.setenv("BOT_DB_DIR", "ฐานข้อมูล")
    result = RunResult(fetched=20, inserted=20, first=date(2026, 10, 1), last=date(2026, 10, 28))
    monkeypatch.setattr(cli, "run", lambda settings: result)
    fake_stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(cli.sys, "stdout", fake_stdout)
    assert cli.main() == 0  # would raise UnicodeEncodeError without errors="replace"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_hint_at_the_token(status):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    with pytest.raises(ApiError, match=f"HTTP {status}.*BOT_API_TOKEN"):
        fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2), client)


def test_other_http_errors_have_no_token_hint():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(ApiError) as exc:
        fetch_daily_rates("tok", date(2026, 1, 1), date(2026, 1, 2), client)
    assert str(exc.value) == "BOT API returned HTTP 500"
