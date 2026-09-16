# naukri-agent

An agentic job-search and application-assistance system for
[Naukri.com](https://www.naukri.com). It discovers jobs, scores them
against your profile with a transparent, deterministic algorithm, and
selects an existing resume file per application — it never generates
or rewords resume content, never submits anything without your
explicit approval, and never attempts to bypass CAPTCHA, MFA, or
anti-bot protections.

> **Status:** Phases 1–6 and Phase 7 Stage 1 (read-only Naukri
> inspection) are done. Phase 7 Stage 2 (write operations — resume
> refresh, apply) has **not** started. See [Development phases](#development-phases)
> for the full table, and `naukri-agent doctor`/`inspect` for the only
> two CLI commands that are actually implemented today — every other
> command is a registered stub that raises `NotImplementedError`
> naming the phase that will implement it.

## Documentation map

| Document | For |
|---|---|
| `README.md` (this file) | Setup, running locally, and the phase-by-phase status table. |
| [`docs/PROJECT_OVERVIEW.md`](docs/PROJECT_OVERVIEW.md) | A deep-dive study guide: architecture, design principles, and a module-by-module code walkthrough — read this to actually *understand* the codebase. |
| `CLAUDE.md` | Live handoff notes for whoever (human or Claude Code) picks up development next. |

## Architecture

```
naukri_agent/
├── config.py           Central validated Settings (env vars / .env) — the only
│                        place that reads os.environ
├── logging_config.py    Rotating file + console logging
│
├── candidate/            CandidateProfile — job-search PREFERENCES (YAML-loaded)
├── resume/               MasterResume (factual career record) +
│                         ResumeRegistry / ResumeSelector (pick an EXISTING file)
├── jobs/                 Job domain models (raw vs. LLM-derived) + JobParser
├── matching/             Deterministic JobScorer and its six category sub-scorers
├── llm/                  Provider-agnostic LLM abstraction (Ollama / Groq / OpenAI)
├── browser/              Playwright + Naukri automation (Stage 1 read-only; done)
├── database/             SQLAlchemy ORM models + repository (upsert) functions
├── cli/                  `naukri-agent <command>` entrypoint
│
├── agents/               (placeholder — future orchestration-level agents)
├── orchestration/        (placeholder — Phase 11's DailyJobPipeline)
├── scheduler/            (placeholder — Phase 11's daily scheduler)
└── notifications/        (placeholder — Phase 10's email summary)
```

The four placeholder packages exist only so the import surface is
stable from the start; they currently hold nothing beyond an
`__init__.py`.

### Browser automation (Phase 7)

```
browser/
├── browser_manager.py    Owns the Playwright lifecycle exclusively
├── selectors.py           The ONLY file with raw CSS selectors — see below
├── login.py                Fills credentials, detects CAPTCHA/MFA, never solves them
├── profile.py                Read-only resume-section inspection
├── jobs.py                    Read-only search + apply-workflow inspection
├── naukri_client.py             High-level facade — the only interface other code should use
└── inspection.py                  The Stage 1 tool: `naukri-agent inspect`
```

Orchestration code should only ever call `NaukriClient`'s methods
(`login()`, `get_profile_resume()`, `search_jobs()`, `get_job()`) —
never a selector directly.

`browser/selectors.py` is a live document: it tracks, selector by
selector, which are `VERIFIED` against a real Stage 1 inspection
capture (with the date and source file cited) and which are still
`UNVERIFIED` best-effort guesses. As of this writing, the resume
section, the job-search card, and the authenticated-session indicator
are verified; `APPLY_BUTTON` and `RESUME_SELECTION_CONTROLS` are not —
no real run has reached an actual job listing's apply workflow yet.
Read that file's own docstring for the current, authoritative state;
this README won't be kept in sync with it selector-by-selector.

### The LLM is used only where language understanding is genuinely useful

Job description parsing and skill extraction go through the LLM
abstraction. Salary matching, experience thresholds, the overall match
score, duplicate detection, and the final accept/reject/review
decision are **deterministic Python** — the LLM never decides whether
an application gets submitted.

### Provider vs. model

`LLM_PROVIDER` (who serves the model: `ollama` / `groq` / `openai`)
and `LLM_MODEL` (which model to ask for: e.g. `llama3.1`,
`llama-3.1-70b-versatile`, `gpt-4o-mini`) are configured
independently. Code depends only on the `LLMProvider` interface —
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

Playwright browsers are only needed for Phase 7 (see below).

## Environment variables

```bash
cp .env.example .env
```

Then edit `.env`. See `.env.example` for the full list with comments.
At minimum you'll need: `DATABASE_URL`, `LOG_DIR`, `LOG_LEVEL`, plus
whichever `LLM_PROVIDER`'s credentials you intend to use.

## Naukri setup

Copy `NAUKRI_EMAIL` / `NAUKRI_PASSWORD` into `.env` — never in source
code, never committed. Required only for `naukri-agent inspect` and
future browser automation.

### Phase 7 Stage 1 — read-only inspection

Some of `browser/selectors.py` is still unverified against Naukri's
real DOM (see above). Before trusting any browser-based Naukri
interaction, run the inspection tool yourself, locally, where you have
real network access and can solve a CAPTCHA/MFA challenge if one
appears:

```bash
playwright install chromium   # one-time, downloads real browser binaries
naukri-agent inspect
```

This performs a read-only walkthrough (login → profile/resume → job
search → one listing) and saves HTML snapshots, screenshots, and a
JSON summary to `./inspection_output/` (gitignored). Nothing is
modified or submitted. Compare the saved HTML against
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
naukri-agent doctor      # verify environment is set up correctly
```

Other commands (`run-now`, `discover`, `match`, `prepare`, `apply`,
`report`, `scheduler`) are registered but not yet implemented — each
raises `NotImplementedError` naming the phase that implements it.

## Dry-run mode

`DRY_RUN=true` in `.env` (the default) means the pipeline will
discover/parse/score/prepare but never submit an application. This
stays the default throughout development.

## Human approval mode

`AUTO_APPLY=false` (the default) means every prepared application
requires your explicit approval before submission. This is not
changed until the system has been thoroughly tested, and even then
only under strict, explicitly-defined safety criteria.

## Scheduler

Not implemented until Phase 11.

## Email setup

Not implemented until Phase 10.

## Testing

```bash
pytest
```

Three tiers, separated so the default run never needs real network or
credentials:
- **Unit tests** — pure logic (matching, models, config).
- **Mocked browser tests** (`test_browser_*.py`) — validate Naukri
  orchestration logic against fake `Page`/`BrowserManager` objects.
- **Manual/real-integration tests** (`tests/manual/`) — require a
  real Naukri account and installed browser binaries; marked
  `@pytest.mark.manual` and excluded by default. Run with
  `pytest -m manual` — see `tests/manual/README.md`.

## Troubleshooting

Run `naukri-agent doctor` first — it checks Python version, config
loading, LLM provider configuration, database connectivity, that your
candidate profile and master resume YAML files exist and parse
correctly, and that every resume registered in `resumes.yaml`
actually exists on disk — reporting exactly which check failed.

## Safety considerations

- Never bypasses CAPTCHA, MFA, or anti-bot mechanisms — pauses and
  asks for human intervention instead.
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
| 7 | Playwright Naukri integration | 🟡 Stage 1 (read-only inspection) done; Stage 2 (write ops) not started |
| 8 | Application preparation | pending |
| 9 | Human approval interface | pending |
| 10 | Email notifications | pending |
| 11 | Daily scheduler | pending |
| 12 | Testing, logging, error handling, docs polish | pending |

Moving to a new phase requires explicit user approval — "looks done"
is never treated as "go ahead" on its own. See `CLAUDE.md` for the
live handoff state and exactly what a Phase 7 Stage 2 kickoff would
require.
