# Midna — Job Finding Agent

Midna (yes, the Twilight Princess imp, in personality only) searches job boards on a rotation, scores every listing against your resume, and sends you only the
best 1–3 over Signal a few times a day. You can reply in Signal to steer it.

## How it works

```
every 90 min   one source from the rotation (keyword sources run 3 rotating queries):
               LinkedIn · Indeed · Glassdoor · ZipRecruiter · Himalayas · Remotive
               company career boards (112 companies, companies.toml) · We Work Remotely · RemoteOK
      │
      ▼
free filters   dedupe (same id, or same company+title on another board) · title blocklist ·
               avoided companies · listed pay below floor · clearly on-site · keyword pre-score
      │
      ▼
Haiku 5.5      screens up to 40 new listings/cycle in batches of 8 (≈ $0.005 per cycle)
      │        score 0–100 with a "read it like a human" rubric (degrees ignored, inflated
      │        years discounted, wish-list requirements treated as wish lists)
      ▼
Opus 5.5       only the shortlist (first-pass ≥ 68) gets a careful final read + a one-line blurb;
      │        it must score ≥ 78 to be sent
      │
      ▼
Signal         digests at 09:00 / 12:30 / 17:30 (max 3 jobs, nothing if nothing qualifies)
               urgent single job if a listing scores ≥ 92 (max 2/day), only 07:00–22:00
```

* **Sources**: LinkedIn/Indeed/Glassdoor go through the `python-jobspy` scraper. ZipRecruiter uses
  our own parser (`fetch_ziprecruiter`), because jobspy 1.4's broke when ZipRecruiter changed its page
  format. Company boards read the public Greenhouse/Lever/Ashby APIs for the companies in
  `companies.toml` (Anthropic, OpenAI, Databricks, Datadog, Stripe, …). Add a company by adding its
  board slug. Test any single source for free with `.venv/bin/python -m job_agent search <source> --no-llm`.
* **Memory**: `data/agent.db` (SQLite) tracks every listing seen and every job sent. A job is sent
  again only if it changes in a meaningful way (new title, new pay range, or a substantially rewritten
  description), and the message labels it "(updated listing)".
* **Understanding your resume**: on first run, Opus reads your resume PDF and writes
  `data/profile.md`. Edit it if anything's off. It is rebuilt only if the resume file changes.
* **Feedback**: `data/preferences.json` holds what you want/avoid. Your Signal replies update it, and
  feedback on specific jobs (`#12 too sales-y`) gets added to the scoring prompt.
* **Cost control**: a hard daily cap (`daily_budget_usd`, default $1.00) on Claude API spend. Expect
  roughly $0.20–0.40/day. Spend is logged in the `llm_usage` table and shown by `status`.

## How picks are chosen

1. Listings scoring ≥ `shortlist_score` (68) on the cheap first pass get a careful read by Opus. Only
   those Opus approves with a final score ≥ `min_score` (78) are eligible to be sent.
2. Ranked by Opus's final score. Opus is told to avoid ties, so its scores decide most of the order.
   Exact ties go to the listing found most recently, then the highest listed pay, then a fixed order
   (never random).
3. At most one job per company in a single message. The other one leads the next message.
4. Strong listings that don't make a message aren't lost. They wait for the next digest (up to 9 a day)
   for as long as the listing is still being seen (`candidate_max_age_days`).
5. **Never sent twice**: a job is blocked only once Signal confirms delivery (a failed send is rolled
   back). The same opening on another board is blocked for `dedupe_window_days` (45). After that, a
   repost counts as new. A sent job comes back only if it materially changes, and never if you said
   it wasn't for you or that you applied.
6. Preference changes apply at send time too (avoided companies, pay floor), not just to new listings.

Run `.venv/bin/python tests/edge_cases.py` to check all of this (17 scenarios, free).

## Setup

1. **Python env** (already done): `~/.local/bin/uv sync`
2. **API key**: `cp .env.example .env`, then set `ANTHROPIC_API_KEY` (from console.anthropic.com).
3. **Try it in console mode** (no Signal yet; messages print to the terminal):
   ```
   .venv/bin/python -m job_agent profile        # builds data/profile.md from your resume
   .venv/bin/python -m job_agent search         # one search + screen cycle
   .venv/bin/python -m job_agent digest         # shows what it would send
   .venv/bin/python -m job_agent say "#1 too junior, and no crypto companies"
   ```
4. **Signal**: the agent links to your own Signal account as an extra device (like Signal Desktop)
   and talks to you in **Note to Self**. It sends with `--notify-self`, so your phone notifies you.
   Java 25 and signal-cli are installed in `~/.local/opt`, behind the wrapper `~/.local/bin/signal-cli`.
   * Put your number in `.env` (`SIGNAL_USER_NUMBER=+1…`).
   * Link: run `~/.local/bin/signal-cli link -n "Job Agent"`, then on your phone go to Signal →
     Settings → Linked devices → + and scan the QR code it prints.
   * `.venv/bin/python -m job_agent test-signal`, then reply `status` in Note to Self.
   * The agent only treats messages **you type into Note to Self** as commands. Its own messages start
     with 🌘 (sent by Midna) and are ignored, and your chats with other people are never read as commands.
   * Prefer a separate bot number? Set `SIGNAL_MODE=bot` and `SIGNAL_BOT_NUMBER`, then register that
     number with signal-cli (captcha required, see the signal-cli wiki).
5. **Run it continuously**:
   ```
   cp com.jobagent.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.jobagent.plist
   ```
   macOS may block background processes from reading `~/Documents`. If `logs/launchd.err.log` shows
   "Operation not permitted", either move this folder out of Documents (and update the plist paths) or
   give `.venv/bin/python` Full Disk Access. The Mac must be awake for the agent to run. For true
   24/7 operation, run it on an always-on machine or a small VPS.

## Running on Windows

Works on Windows 10/11 on an x64 PC (signal-cli's native library has no Windows-on-ARM build).

1. `git clone https://github.com/Bjorklundsamm/job-hunting-agent.git`, then copy `.env`, `data/` and
   your resume PDF into the folder from the old machine. `data/` is the agent's memory of what it has
   sent, so don't skip it.
2. In PowerShell, in the repo folder: `powershell -ExecutionPolicy Bypass -File windows\setup.ps1`.
   This installs uv, Java 25 and signal-cli, then the Python environment. Put the `SIGNAL_CLI_PATH=`
   line it prints into `.env`.
3. Link Signal: on your phone, remove the old machine under Signal → Settings → Linked devices. Then
   run `& "$env:LOCALAPPDATA\signal-cli\signal-cli-0.14.9\bin\signal-cli.bat" link -n Midna` and scan
   the `sgnl://` link as a QR code (paste it into any QR generator, or ask Claude Code to render it).
   Afterwards run one `... -a +1YOURNUMBER receive` to finish syncing.
4. `.venv\Scripts\python -m job_agent test-signal`, then reply `status` in Note to Self.
5. Background: `powershell -ExecutionPolicy Bypass -File windows\install-task.ps1`. This registers a
   scheduled task, "Midna", that starts at logon and restarts if it crashes. Turn off sleep (Settings →
   System → Power) if you want it searching around the clock.

## Talking to it on Signal

| Message | Effect |
|---|---|
| `more` | send picks right now |
| `pause` / `pause 48` | stop messages for 12h / 48h |
| `resume` | un-pause |
| `status` | listings seen, queue size, today's spend |
| `prefs` | what it's currently looking for |
| anything else | feedback, e.g. `#7 applied!`, `#3 too sales-heavy`, `no more gov contractors`, `raise my floor to 90k` |

## Tuning

Everything static is in `config.toml`: schedule, thresholds, sources/queries rotation, models, budget.
If you get too few picks, lower `min_score` (e.g. 72). If you get too many, raise it.
`screen_model`/`judge_model` can be switched (e.g. judge on `claude-sonnet-5-5` to cut cost further).
