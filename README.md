# naukri-agent

An agentic job-search assistant for Naukri.com. Discovers jobs, scores
them against your profile with a transparent deterministic algorithm,
selects the best-fitting resume from your own files, and emails you a
ranked daily digest of the strongest matches with their Naukri links
and concise reasons. It never submits an application, and never
attempts to bypass CAPTCHA, MFA, or anti-bot protections (Section 20).

> **Objective change (2026-09-09):** automatic application submission
> was dropped. The goal is now a **daily match digest** — discover,
> understand, rank, and email the best jobs; you apply on Naukri
> yourself and record it with `naukri-agent mark-applied`. This is
> built as **Stage A** (implemented). The Apply-workflow
> reverse-engineering (Stage 1.5) is frozen in the tree, untouched.

> **Status:** Stage A (daily match digest) and Stage B (real SMTP
> email + in-process scheduler) are both implemented — read-only
> discovery, deterministic ranking, application/cooldown-aware
> filtering, manual application history, file/console/SMTP digest
> delivery, 3-sheet Excel mirror, and `naukri-agent scheduler` for
> daily automation without relying on an OS-level cron entry. `EMAIL_
> SENDER` still defaults to `file` — switching to `smtp` is a
> deliberate opt-in step (see "Email setup" below).

## Architecture

```
naukri_agent/
├── config.py            # Central validated settings (env vars + .env)
├── logging_config.py    # Rotating file + console logging
├── llm/                 # Provider-agnostic LLM abstraction (Phase 5)
│   ├── base.py              LLMProvider interface
│   ├── factory.py           reads LLM_PROVIDER/LLM_MODEL, builds the right one
│   └── providers/            OpenAIProvider, GroqProvider, OllamaProvider
├── candidate/            # CandidateProfile model (Phase 2)
├── resume/                # MasterResume (Phase 2); ResumeRegistry + selection (Phase 6)
├── jobs/                  # JobDiscovery, JobParser (Phase 3, 5)
├── matching/               # Deterministic scoring engine (Phase 4)
├── agents/                  # (empty placeholder — unused)
├── browser/                  # Playwright + Naukri client (Stage 1 read-only; Stage 1.5 frozen)
├── database/                  # SQLAlchemy models + session management
├── recommendations/            # build_digest: rank/filter JobMatch rows for the daily email
├── reporting/                    # excel.py: 3-sheet workbook regenerated from the DB
├── notifications/                  # FileEmailSender/ConsoleEmailSender/SmtpEmailSender
├── orchestration/                    # discovery.py + pipeline.py: run_daily_recommendations
├── scheduler/                          # Stage B: naukri-agent scheduler (daemon.py)
└── cli/                                  # `naukri-agent <command>` entrypoint
```

### Browser automation (Phase 7)

```
browser/
├── browser_manager.py   # owns the Playwright lifecycle exclusively
├── selectors.py          # the ONLY file with raw CSS selectors — currently UNVERIFIED
├── login.py                # fills credentials, detects CAPTCHA/MFA, never solves them
├── profile.py                # read-only resume-section inspection
├── jobs.py                     # read-only search + apply-workflow inspection
├── naukri_client.py              # high-level facade — the only interface other code should use
└── inspection.py                   # the Stage 1 tool: `naukri-agent inspect`
```

Orchestration code should only ever call `NaukriClient`'s methods
(`login()`, `get_profile_resume()`, `search_jobs()`, `get_job()`) —
never a selector directly.

### The LLM is used only where language understanding is genuinely useful

Job description parsing, skill extraction, resume tailoring, and
interpreting free-text application questions go through the LLM
abstraction. Salary matching, experience thresholds, the overall
match score, duplicate detection, and the final accept/reject/review
decision are **deterministic Python** — the LLM never decides whether
an application gets submitted.

### Provider vs. model

`LLM_PROVIDER` (who serves the model: `ollama` / `groq` / `openai`)
and `LLM_MODEL` (which model to ask for: e.g. `llama3.1`,
`llama-3.1-70b-versatile`, `gpt-4o-mini`) are configured
independently. Agents depend only on the `LLMProvider` interface —
never on a specific provider's SDK.

## Installation

Requires **Python 3.12+**.

```bash
git clone <repo-url>
cd Agentic-Job-Search-Application-Automation-System
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Playwright browsers are installed in Phase 7 — not needed yet.

## Environment variables

```bash
cp .env.example .env
```

Then edit `.env`. See `.env.example` for the full list with comments.
At minimum for Phase 1: `DATABASE_URL`, `LOG_DIR`, `LOG_LEVEL`.

## Naukri setup

Copy `NAUKRI_EMAIL` / `NAUKRI_PASSWORD` into `.env` — never in source
code, never committed. Required only for `naukri-agent inspect` and
future browser automation (Phase 7+).

### Phase 7 Stage 1 — read-only inspection

This system's Naukri selectors (`browser/selectors.py`) are
**unverified placeholders** — this codebase has no live access to
naukri.com to confirm them against. Before trusting any browser-based
Naukri interaction, run the inspection tool yourself, locally, where
you have real network access and can solve a CAPTCHA/MFA challenge if
one appears:

```bash
playwright install chromium   # one-time, downloads real browser binaries
naukri-agent inspect
```

This performs a read-only walkthrough (login → profile/resume →
job search → one listing's apply workflow) and saves HTML snapshots,
screenshots, and a JSON summary to `./inspection_output/` (gitignored).
Nothing is modified or submitted. Compare the saved HTML against
`browser/selectors.py` and update it with what you actually find —
that's the only file that should ever need editing when Naukri's UI
changes.

## Candidate configuration

```bash
cp config/candidate_profile.example.yaml config/candidate_profile.yaml
```

Edit `candidate_profile.yaml` with your real job-search criteria
(skills, preferred roles/locations, salary expectations, notice
period, etc.). This file is gitignored. See the file itself for field
documentation.

## Resume configuration

```bash
cp config/master_resume.example.yaml config/master_resume.yaml
```

Edit `master_resume.yaml` with your real career history — used for
matching and as a factual record, not for generating any document.
This file is gitignored.

## Resume registry (Phase 6)

This system does **not** generate or edit resumes. You maintain your
own resume files (e.g. `resumes/data_scientist.pdf`) and register
them:

```bash
cp config/resumes.example.yaml config/resumes.yaml
```

Edit `resumes.yaml` to list your actual files and the roles each one
covers. `naukri-agent doctor` verifies every registered file exists.
Both `resumes.yaml` and the `resumes/` directory are gitignored.

## Running locally

```bash
naukri-agent doctor        # verify environment is set up correctly
naukri-agent run-daily     # discover -> understand -> rank -> digest (+ Excel); one-shot
naukri-agent discover --query "Data Scientist @ Pune"   # read-only discovery only
naukri-agent recommend --dry-run                        # full pipeline, digest to file
naukri-agent export-excel --path ./out/history.xlsx     # regenerate the workbook from the DB
naukri-agent mark-applied <job-id|external-id|url> --status APPLIED --note "applied on portal"
naukri-agent mark-status <job> INTERVIEW
naukri-agent applications --status APPLIED              # the authoritative application history
naukri-agent report                                    # re-print the latest digest
naukri-agent scheduler                                 # Stage B: run run-daily automatically, once a day, forever (Ctrl+C to stop)
```

`prepare` / `apply` raise `NotImplementedError` — application
submission is out of scope; use `mark-applied` to record one yourself.

## Dry-run mode

`DRY_RUN=true` in `.env` (the default) means the daily pipeline
discovers / parses / scores / ranks and writes the digest to a file,
but never sends email. Stage A has no code path that sends real email
or submits an application.

## Human approval mode

`AUTO_APPLY=false` (the default) means every prepared application
requires your explicit approval before submission. This is not
changed until the system has been thoroughly tested, and even then
only under strict, explicitly-defined safety criteria.

## Daily match digest (Stage A)

`run-daily` runs the whole business logic once (no scheduling):
discover jobs (read-only), understand each JD, score deterministically
against your `CandidateProfile` + `MasterResume`, rank, cap at
`DAILY_RECOMMENDATION_LIMIT` (default 10), render the digest to a file,
and regenerate `job_search_history.xlsx` from the database.

- **The database is the source of truth.** Excel is a read-only mirror,
  fully regenerated on every export.
- **Application status is only ever what you recorded** via
  `mark-applied` / `mark-status` — never inferred, never asked of the
  LLM. The Naukri link in the digest always comes from the stored `Job`.
- A job you have applied to (or any status in
  `RECOMMENDATION_EXCLUDE_IF_STATUS`) is never recommended again. A job
  previously recommended but not applied follows
  `RECOMMENDATION_COOLDOWN_DAYS`: `0` = eligible again next run; `>0` =
  wait that many days.
- The LLM only interprets job descriptions (extract-only) and, if
  `EXPLANATION_USE_LLM=true`, adds an optional narrative sentence. It
  never sets a score, decision, status, or URL.

## Scheduler

Two equally valid ways to run the daily digest automatically — pick
one, don't run both against the same database:

1. **`naukri-agent scheduler` (Stage B, in-process)** — a single
   foreground command (`scheduler/daemon.py`, built on APScheduler)
   that blocks forever and fires `run_daily_recommendations` once a
   day at `DAILY_RUN_TIME` (default `10:00`, 24-hour) in your
   configured `TIMEZONE`. One bad day's run is logged and swallowed —
   it never cancels tomorrow's firing. Stop it with Ctrl+C. This is
   the simplest cross-platform option and needs nothing beyond keeping
   one process alive (a terminal, `screen`/`tmux`, a system service
   unit, etc.).
2. **An OS-level scheduler** (Windows Task Scheduler / cron) invoking
   `naukri-agent run-daily` directly on its own schedule — no
   long-lived `naukri-agent` process at all between runs. This was the
   original approach (validated in Run 23) and remains fully
   supported; see the Windows walkthrough below if you'd rather not
   keep a process running continuously.

Both paths call the exact same `run_daily_recommendations` business
logic in `orchestration/pipeline.py` — neither is "more correct" than
the other.

### Windows Task Scheduler setup (daily 10:00 AM IST)

Two hard requirements, both because `naukri-agent`'s config/paths
(`.env`, `./data`, `./logs`, `./out`, `./config`) are resolved relative
to the process's **working directory**, not its own file location:

1. **"Start in" must be the project root** — exactly the folder
   containing `.env`, `pyproject.toml`, `data/`, `config/`. If this is
   wrong, `.env` silently isn't found and every setting (Naukri
   credentials included) falls back to an empty/default value.
2. **The action must run `.venv\Scripts\naukri-agent.exe run-daily`**
   (the installed console-script entry point) — not a bare `python`
   invocation of some other module — because that entry point is the
   only one that calls `setup_logging()` before anything else runs
   (see `cli/main.py:main`), which is what makes a scheduled run's
   logs land in `logs/naukri_agent.log` instead of nowhere.

**Create the task** (PowerShell, run once, interactively, to register
it — this does not execute the task, just creates it):

```powershell
$action = New-ScheduledTaskAction `
  -Execute "C:\Users\sneha\Desktop\hemant\claude\projects\naukri_agent_project\naukri_agent\.venv\Scripts\naukri-agent.exe" `
  -Argument "run-daily" `
  -WorkingDirectory "C:\Users\sneha\Desktop\hemant\claude\projects\naukri_agent_project\naukri_agent"
$trigger = New-ScheduledTaskTrigger -Daily -At 10:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd
Register-ScheduledTask -TaskName "NaukriAgentDailyDigest" -Action $action -Trigger $trigger -Settings $settings -Description "naukri-agent run-daily, read-only discovery + digest, no Apply"
```

`-Daily -At 10:00AM` uses the machine's local timezone — set Windows'
own timezone to India Standard Time (Settings, or already the case if
you're in India) rather than trying to convert in the task itself;
Task Scheduler has no separate per-task timezone concept.

**Ollama must already be running at 10:00 AM.** On this machine Ollama
runs as a normal per-user app (`ollama app.exe`, auto-started at
login), **not** a Windows service — it is only alive while you are
logged in. Two ways to make this reliable, in order of preference:
- Leave the task at its default trigger settings (equivalent to "only
  run when a user is logged on") and stay logged in / don't fully shut
  down the machine overnight — the simplest option, and what was just
  validated in Run 23.
- If you need it to run even when logged out, you'd need Ollama
  installed/configured as an actual Windows service first (not covered
  here — out of scope for this change, since it touches a piece of
  software outside this repo).

Do **not** check "Run whether user is logged on or not" unless Ollama
is guaranteed to be running independently of your session — otherwise
JD extraction will fail with a connection error to `localhost:11434`
every time.

**What happens if Naukri needs a human** (CAPTCHA/MFA, or the
persistent session simply expired): `login()` raises
`NaukriCaptchaError`/`NaukriMfaError` immediately — it never blocks on
input, so a scheduled run can never hang waiting for someone to solve
a challenge. `orchestration/pipeline.py` catches any such exception
around discovery and marks the `DailyRun` `FAILED` with the exception
type recorded, so a failed scheduled run is always visible (check
`naukri-agent doctor`'s "Digest tables reachable" count, `logs/
naukri_agent.log`, or query `RunEvent`) — Task Scheduler's own History
tab will also show the process's exit code.

**Verify without waiting for 10:00 AM:** right-click the task in Task
Scheduler → Run. This executes the real command immediately, so only
do this once you're ready for a real run (same considerations as
running `naukri-agent run-daily` by hand).

## Email setup

Digest delivery has three modes, selected by `EMAIL_SENDER`:

- **`file`** (default) — writes the digest under `EMAIL_OUTPUT_DIR`
  (`./out/emails/digest_<timestamp>.txt`). Nothing is sent anywhere.
  This is what every run so far, including Run 23, has used.
- **`console`** — prints the digest to stdout.
- **`smtp`** — sends a real email via `SmtpEmailSender`
  (`notifications/email.py`), stdlib `smtplib` + STARTTLS. Requires
  `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `NOTIFY_EMAIL_TO`
  to ALL be set in `.env` — if any are missing, the run fails clearly
  (`EmailConfigError`, itself a subclass of the existing
  `EmailSendError`) instead of silently falling back to a file, so a
  broken schedule shows up as a failed run, not a quiet no-op.

**For Gmail specifically:** `SMTP_HOST=smtp.gmail.com`,
`SMTP_PORT=587`, `SMTP_USERNAME=<your gmail address>`, and
`SMTP_PASSWORD=<a Gmail App Password>` — not your normal account
password. Generate one at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
(requires 2-Step Verification enabled first). Any other standard
STARTTLS SMTP provider works the same way; nothing here is
Gmail-specific beyond that example.

Credentials are read only from `.env` (via `Settings`) — never
hard-coded, never logged. Every `SmtpEmailSender` failure is reported
by exception type only (`type(exc).__name__`), matching
`FileEmailSender`'s existing convention, since some SMTP server error
responses can echo back parts of the failed request.

**`EMAIL_SENDER` is still `file` by default** — switching to `smtp` is
a deliberate, separate step you take when ready; it was not flipped as
part of adding this capability.

## Testing

```bash
pytest
```

Three tiers, separated so the default run never needs real network or
credentials:
- **Unit tests** — pure logic (matching, models, config).
- **Mocked browser tests** (`test_browser_*.py`) — validate Naukri
  orchestration logic against fake Page objects.
- **Manual/real-integration tests** (`tests/manual/`) — require a
  real Naukri account and installed browser binaries; marked
  `@pytest.mark.manual` and excluded by default. Run with
  `pytest -m manual` — see `tests/manual/README.md`.

Stage A added tests for recommendation ranking + cooldown semantics,
application history, digest/Excel output, read-only job-detail fetch,
end-to-end pipeline, and new-table migration; Stage B added tests for
the scheduler's job configuration and its safe-failure wrapper. Full
non-manual suite: 847 passed, 3 deselected.

## Troubleshooting

Run `naukri-agent doctor` first — it checks Python version, config
loading, LLM provider configuration, database connectivity, that your
candidate profile and master resume YAML files exist and parse
correctly, and that every resume registered in `resumes.yaml`
actually exists on disk — reporting exactly which check failed.

## Safety considerations

- Never bypasses CAPTCHA, MFA, or anti-bot mechanisms — pauses and
  asks for human intervention instead (Section 20).
- Never submits an application without explicit human approval unless
  `AUTO_APPLY` is deliberately enabled after thorough testing.
- Never fabricates salary, experience, employment history, notice
  period, qualifications, legal declarations, or demographic
  information.
- Credentials and API keys live only in `.env` / environment
  variables, never in source or committed config.

## Development phases

| Phase | Contents | Status |
|---|---|---|
| 1 | Scaffolding, config, database, logging, CLI skeleton | ✅ done |
| 2 | Candidate profile + master resume models | ✅ done |
| 3 | Job model + storage + deduplication | ✅ done |
| 4 | Deterministic job matching engine | ✅ done |
| 5 | LLM-based job description parser + provider abstraction | ✅ done |
| 6 | Resume registry + selection (revised from resume-tailoring) | ✅ done |
| 7 | Playwright Naukri integration | 🟡 Stage 1 (read-only inspection) done; read-only job-detail fetch added for Stage A; Stage 1.5 Apply-workflow frozen |
| 8 | Application preparation | ⛔ abandoned — application submission is out of scope |
| 9 | Human approval interface | ⛔ abandoned |
| A | Daily match digest (objective change) | ✅ done — discovery + JD fetch, deterministic ranking, ApplicationHistory + manual `mark-applied`, file/console digest, 3-sheet Excel, `RunEvent` audit |
| B | Real SMTP email + scheduler | ✅ done — `SmtpEmailSender` (opt-in via `EMAIL_SENDER=smtp`) and `naukri-agent scheduler` (in-process daily trigger); OS-level cron/Task Scheduler remains a supported alternative to the latter |
| 12 | Testing, logging, error handling, docs polish | pending |
