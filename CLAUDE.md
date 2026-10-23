# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Midna: a long-running Python agent that scrapes job boards on a rotation, scores listings against the
user's resume with Claude (cheap screen model → expensive judge model), and sends the best 1–3 over
Signal via `signal-cli`. The user steers it by replying in Signal. README.md has the user-facing
behaviour spec (thresholds, send rules, Signal commands); keep it in sync when changing behaviour.

## Commands

Environment is managed with `uv` (`uv sync`). Python ≥ 3.12. The interpreter is `.venv/bin/python` on
macOS and `.venv\Scripts\python` on Windows (this checkout is on Windows).

```
python -m job_agent search <source> --no-llm   # one source, no LLM spend: use this to check a scraper
python -m job_agent search [source]            # search + screen + urgent check (spends LLM budget)
python -m job_agent digest                     # force a digest now
python -m job_agent say "<text>"               # handle text as if it came from Signal
python -m job_agent status | profile | test-signal | run
python tests/edge_cases.py                     # the test suite (see below)
```

Without `SIGNAL_USER_NUMBER` in `.env`, Signal runs in console mode and messages are printed instead of
sent. Note: the CLI uses the real `data/agent.db`, so `search`/`digest`/`say` mutate live state
(rotation indexes, sent rows, preferences).

### Tests

There is no pytest. `tests/edge_cases.py` is a single script of numbered scenarios printing `PASS`/`FAIL`
lines (it does not set a non-zero exit code, so read the output). It runs against a temp SQLite DB and
temp preferences file, monkeypatches `LLM.parse` (driven by the `SCREEN`/`JUDGE` dicts keyed by job id)
and `signal.send`, so it is free. Scenarios share state and run in order; you can't run one in isolation.
It loads the real `config.toml`, so changing thresholds there (e.g. `min_score`, `dedupe_window_days`)
can change expected results; `max_per_digest` is overridden to 3 at the top so the multi-job scenarios
keep working (scenario 12 sets it back to 1).

## Architecture

`__main__.py` (CLI) → `Agent` (`agent.py`) owns `DB`, `LLM`, `Signal`, and `Pipeline`.

- **`agent.py`** — the scheduler loop (`run_forever`: poll Signal inbox, run a search cycle every
  `search_interval_minutes`, fire digests at `digest_times`), message handling (fixed commands like
  `more`/`pause`/`status`, everything else goes to `_feedback`: one judge-model call that rewrites
  `Preferences`, records per-job feedback, and can answer resume questions. Its system prompt embeds
  the resume text (`profile.resume_text`, re-read from the PDF each message) and `profile.md`; full
  listings for any `#N` in the message go in the user turn), and sending. `send_jobs` inserts `sent` rows inside a `BEGIN IMMEDIATE`
  transaction and rolls back if Signal delivery fails — a job only counts as sent once delivered, and
  the `sent.ref` autoincrement is the `#N` the user sees and references in feedback.
- **`pipeline.py`** — search → ingest → screen → judge → pick. Job lifecycle lives in `jobs.status`
  (`new` → `screened` | `rejected` | `filtered` | `duplicate`) plus `judged`/`score`/`blurb`.
  Key concepts:
  - Rotation state (`cycle_index`, `query_index`) is stored in the `kv` table, not memory.
  - `ingest` dedupes by id (`site:native_id`), then by `fingerprint` (normalized company+title) within
    `dedupe_window_days`; a re-seen listing is reset to `new` only if `filters.materially_changed`,
    and marked `updated` if it was already sent.
  - Screening batches `new` rows (best `prescore` first) to `screen_model`; judging sends shortlisted
    rows to `judge_model`. Listings the model fails on or omits get `attempts` bumped and are retired
    after `MAX_ATTEMPTS`.
  - `pick_digest`/`pick_urgent` re-apply current preferences at send time, rank by `RANK_ORDER`
    (deterministic tiebreaks), and enforce one job per company per message (`_diverse`).
  - `BudgetExceeded`/`LLMFailed` are caught so a judge outage still lets already-approved jobs send.
- **`llm.py`** — the only place the Anthropic SDK is called. `LLM.parse` does structured output via
  Pydantic schemas, caches the system prompt, records cost per call in `llm_usage` (using the
  `PRICES` table — add new models there), and raises `BudgetExceeded` once `daily_budget_usd` is hit.
  Opus/Sonnet calls use the server-side refusal fallback beta.
- **`prompts.py`** — scoring rubric and feedback prompt. `scoring_system` embeds profile, prefs and
  feedback lines and must stay byte-identical across calls within a cycle for prompt caching.
- **`sources.py`** — one fetcher per board, all normalized to the same dict shape (see module
  docstring). Query sources (`QUERY_SOURCES`) run once per query; feed sources (`FEED_SOURCES`) pull
  the whole feed. Fetchers may return `{"id", "stub": True}` meaning "still listed, unchanged".
  `fetch()` swallows exceptions so one broken scraper never stops the agent. Company boards
  (Greenhouse/Lever/Ashby slugs from `companies.toml`) are fetched in threads and stored with
  `site` = `greenhouse`/`lever`/`ashby`, not `company_boards`; only Greenhouse uses the `known`
  snapshot to return stubs.
- **`filters.py`** — free, pre-LLM logic: hard filters (title blocklist, avoided companies, pay floor,
  non-US/on-site), keyword prescore, description trimming, fingerprint/content hash.
- **`profile.py`** — builds `data/profile.md` from the resume PDF via the judge model; rebuilt only when
  the resume hash changes.
- **`self_update.py`** — code changes requested over Signal. When `_feedback`'s model sets
  `code_change`, the user's verbatim message goes to headless `claude -p` (file tools only), then
  `tests/edge_cases.py` gates it (one retry), then it is committed and pushed. Failures are stashed.
  On success the worker exits with `RESTART_CODE` and the `run` supervisor in `__main__.py` (parent
  process, `MIDNA_WORKER` unset) starts a fresh worker. So `run` is always two processes, and whatever
  lands on `main` here goes live on the next restart.
- **`signal_client.py`** — wraps the `signal-cli` subprocess. In `note_to_self` mode the agent and user
  share an account, so inbound messages are accepted only if they're self-syncs, not one of the agent's
  own sent timestamps, and don't start with `MARKER` (🌘). Every outgoing message is prefixed with it.

### Config and state

- `config.toml` — static settings, accessed as attributes on `Settings` (e.g. `cfg.delivery["min_score"]`,
  `cfg.llm[...]`) via `__getattr__` over the raw TOML. Restart required to apply.
- `.env` — `ANTHROPIC_API_KEY`, `SIGNAL_MODE`, `SIGNAL_USER_NUMBER`, `SIGNAL_BOT_NUMBER`, `SIGNAL_CLI_PATH`.
- `data/` (gitignored) — `agent.db` (SQLite, WAL; schema in `db.py`, additive migrations in
  `DB._migrate`), `preferences.json` (seeded from `DEFAULT_PREFERENCES`, rewritten by Signal feedback),
  `profile.md`. This is the agent's memory of what it has sent — never delete it casually.
- `logs/agent.log` — rotating log.

### Platform notes

Runs under launchd on macOS (`com.jobagent.plist`) or Task Scheduler on Windows
(`windows/setup.ps1`, `windows/install-task.ps1`, run windowless via `pythonw`, so `sys.stderr` may be
`None`). All file and subprocess I/O passes explicit UTF-8 encoding because Windows consoles default to a
non-UTF-8 code page and messages contain emoji — keep doing this.
