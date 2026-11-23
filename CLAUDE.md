# CLAUDE.md

Instructions for AI agents working in this repository.

## What this project does
`uv run bot-fx` fetches the BOT daily THB/USD weighted-average interbank rate, keeps the latest
20 data points, and appends new ones to `.venv/bot_api_daily_{APPLICANT_NAME}.db` (SQLite).

## Commands
- Install: `uv sync`
- Run: `uv run bot-fx`
- Test: `uv run pytest` (offline; HTTP is mocked with `httpx.MockTransport`)

## Hard rules
- **Never commit or print secrets.** `BOT_API_TOKEN` lives only in `.env` (git-ignored). Do not
  log request headers/URLs with credentials; `client.py` deliberately raises `ApiError` with the
  status code (plus a non-sensitive hint for 401/403) only, and keeps the token out of
  `__traceback__` frame locals too (see `_attempt_request`'s docstring in client.py).
- Keep `.env`, `.venv/`, `*.db` out of git.
- Use `uv` for everything (no `pip install` into the project).

## Facts about the API (verified against the live endpoint)
- Auth: raw token in the `Authorization` header (no `Bearer` prefix).
- Query params: `start_period`, `end_period` as `YYYY-MM-DD`.
- Ranges of ~35+ calendar days return HTTP 400 (31 days OK) -> `workflow._collect` fetches in
  30-day windows walking backwards.
- Only business days have rows; response is newest-first; `rate` is a string like `"33.8870000"`.
- "20 days" in the brief is interpreted as the **20 most recent data points**.

## Conventions
- Python >= 3.11, type hints, small modules (config / client / storage / workflow / cli).
- Storage must stay idempotent: `period` is the primary key and inserts use `INSERT ... ON CONFLICT(period) DO NOTHING` (not `INSERT OR IGNORE`, which would also swallow NOT NULL violations).
- Add or update a test in `tests/` for every behavior change.
- Keep `README.md`, `docs/guide.html` and `NOTES.md` consistent with the code.
