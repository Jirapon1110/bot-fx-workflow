import pytest

from bot_fx import config


@pytest.fixture(autouse=True)
def _isolate_from_project_env(monkeypatch, tmp_path_factory):
    """Never let a test pick up the developer's real project .env (it holds a real token).

    Patching config.PROJECT_ROOT alone is not enough: _find_env_file() falls back to
    find_dotenv(usecwd=True), which depends on the process cwd, not on PROJECT_ROOT.
    So this also chdirs to an empty, isolated directory by default. A test that needs
    a specific cwd (e.g. to set up its own fake .env) still works: it just calls
    monkeypatch.chdir(...) itself afterward, which simply overrides this default.
    """
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path_factory.mktemp("no_project"))
    monkeypatch.chdir(tmp_path_factory.mktemp("no_cwd"))
