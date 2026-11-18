# bot-fx-workflow

Python workflow that pulls the **Weighted-average Interbank Exchange Rate (THB / USD)** from the
Bank of Thailand API and appends the latest **20 daily data points** to a SQLite database.

- Endpoint: `https://gateway.api.bot.or.th/Stat-ReferenceRate/v2/DAILY_REF_RATE/`
- Output: `.venv/bot_api_daily_{APPLICANT_NAME}.db`, table `daily_ref_rate`
- User guide (Thai, HTML): [`docs/guide.html`](docs/guide.html)

## Requirements

- [`uv`](https://docs.astral.sh/uv/) (Python >= 3.11 is managed/used by uv)
- A free API token from <https://portal.api.bot.or.th> (register, create an app, subscribe to
  *Exchange Rates*)

## Setup

```bash
uv sync
cp .env.example .env        # Windows: copy .env.example .env
# edit .env: set BOT_API_TOKEN and APPLICANT_NAME (English name, letters/digits/_/- only)
```

`.env` is git-ignored. **Never commit the token.**

## Run

```bash
uv run bot-fx
```

Example output:

```
Fetched 20 points (2026-10-21 .. 2026-11-18); inserted 20 new rows into .venv/bot_api_daily_Name.db
```

Running it again appends only days that are not stored yet (`period` is the primary key), so it
is safe to schedule daily.

## Configuration

| Variable         | Required | Meaning                                                    |
|------------------|----------|------------------------------------------------------------|
| `BOT_API_TOKEN`  | yes      | Token from the BOT portal                                  |
| `APPLICANT_NAME` | yes      | English name used in the DB file name                      |
| `BOT_DB_DIR`     | no       | Directory for the DB file (default `.venv`)                |

## Tests

```bash
uv run pytest
```

Tests use `httpx.MockTransport`; they never call the real API or need a token.

## Layout

```
src/bot_fx/
  config.py     settings from env / .env
  client.py     API call + response parsing
  storage.py    SQLite schema + idempotent append
  workflow.py   orchestration (windowed fetch -> latest 20 -> store)
  cli.py        `bot-fx` entry point
tests/          pytest suite
docs/guide.html user guide (Thai)
NOTES.md        interview notes: understanding, ambiguities, workflow
```
