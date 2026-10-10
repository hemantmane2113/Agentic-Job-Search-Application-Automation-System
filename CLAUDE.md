# naukri-agent: project context for Claude Code

Read automatically at the start of every session. It states what the project is, the rules that must never be broken, how the
code is laid out, and how to work with this user. For the full picture see `docs/PROJECT_OVERVIEW.md`; for setup, `README.md`.
Detailed dated history of every real-Naukri fix lives in git history, the module docstrings and the VERIFIED/UNVERIFIED notes in
`src/naukri_agent/browser/selectors.py`, not here.

## What this is

A personal job-search assistant for Naukri.com, run on the user's Windows PC. Every day it finds fresh jobs, reads them, scores them
with plain code against a candidate profile and resumes, emails a digest, and tells the phone about jobs it can apply to. It applies
to **Naukri Apply jobs only after the user taps Yes on Telegram, one job at a time**. For company-website jobs it gives the direct
link, researches the company with a read-only AI agent, and asks on Telegram whether the user applied. A weekly Excel report goes
out every Sunday at 22:00. The user starts applying from a phone with `/apply`.

## Ground rules that must never be broken

- **A human approves every application.** Nothing is applied without the user's Yes tap on that job; silence, No or a timeout means
  no. `/apply` only starts the run that asks. The daily run, discovery, the scheduler, the research agent, the phone listener and the
  follow-up never import apply code (structural tests enforce it).
- **Never bypass CAPTCHA, MFA, rate limits or anti-bot protection.** Detect and pause for a human. No stealth, no evasion. A site that
  blocks a plain visitor is left alone (a LinkedIn browser, a Cloudflare-protected job API).
- **Never press "Apply on company site".** Naukri marks the job Applied on the account when it is pressed. The employer's address is
  read from the job data the page already loads (`browser/company_link.py`), passively.
- **`DRY_RUN=true` and `AUTO_APPLY=false` stay the defaults in `.env`.** `telegram-apply` needs both flipped, for that one process
  only; the phone listener sets them for the child process it starts, never for itself.
- **The LLM never decides.** Scoring, ranking, eligibility, statuses and the daily plan are deterministic code. The parser only
  extracts; the research agent only reads. Code, not a model, writes the research sources, the apply route and the direct link.
- **The database is the truth.** `ApplicationHistory` is the only authoritative record of what the user did. Excel files are copies
  rebuilt from the database. Never infer an application, never ask a model.
- **No resume generation.** `resume/` selects among the user's own files only.
- **`browser/selectors.py` is the only file with a raw CSS/XPath selector.** Mark each VERIFIED (with the date and source) or
  UNVERIFIED. Do not present a guess as confirmed.
- **Skill normalisation is a small alias table, never capability inference** ("Python" never implies "Django"). The LLM-based
  Tier 3 semantic matcher stays disabled.
- **Secrets stay out of everything.** Never paste or print keys or passwords. `.env`, `config/*.yaml` (not the `*.example` files),
  `resumes/`, `data/`, `logs/`, `out/`, `inspection_output/` are git-ignored. Errors report only the exception type.
- **Real email only with `EMAIL_SENDER=smtp`.** It fails loudly if any SMTP setting is missing.
- **Back up `data/naukri_agent.db` before any manual change to it** (`cp data/naukri_agent.db data/naukri_agent.db.bakNN-HHMM`).

## Layout

```
src/naukri_agent/
  config.py  power.py            settings; keep Windows awake during long jobs
  candidate/ resume/ jobs/       profile and resumes; job models and the LLM parser; skill evidence
  matching/                      deterministic scorer: skills 35, experience 20, role 15, salary 11, location 7, education 5,
                                 posted-recently 7. ACCEPT >= 80, REVIEW >= 70
  llm/                           Ollama (local, parsing), Groq (research), OpenAI
  browser/                       Playwright: login, search, job pages, profile upload, the apply panel (apply_workflow.py),
                                 company_link.py, apply_inspection.py (the default-deny network guard still used when applying)
  database/                      SQLAlchemy models and repositories (SQLite; new nullable columns/tables are added at startup)
  orchestration/                 pipeline.py (daily run), discovery.py, auto_apply_runner.py, telegram_listener.py, followup.py,
                                 profile_refresh.py, watchdog.py, apply_lock.py
  recommendations/               build_digest (Parts 1-3), apply_ready.py, employment.py
  research_agent/                read-only company-site researcher and ATS detection
  notifications/ reporting/      email (file/console/SMTP with attachments), Telegram, Excel mirror, weekly report
  agents/  scheduler/  legacy/   profile answers (no AI); optional in-process scheduler; older commands kept but unused
  cli/                           `naukri-agent <command>`
tests/                           94+ files; tests/manual/ needs a real Naukri account; tests/browser_fakes.py fakes Playwright
docs/                            PROJECT_OVERVIEW.md and the architecture/workflow HTML pages
```

## What runs when (Windows Task Scheduler, user signed in)

09:45 profile refresh · 10:00 `run-daily` · 10:05 `research-jobs --wait-for-digest` · 10:30 and 15:00 `watchdog` · Sunday 22:00
`weekly-report` · at logon `telegram-listen` (phone control, always on). The 20:00 "did you apply?" question is sent by the listener.

## Things to know before changing anything

- **The apply panel is the fragile part.** Naukri's chat widget varies: question types (radio `label.ssrc__label`, checkbox
  `label.mcc__label`, free text in the drawer), order, and skipped questions. After any apply failure read
  `inspection_output/auto_apply/<attempt>_failure.{png,html}` before guessing. Any new type should produce a clean stop with a snapshot.
- **Do not change `browser/apply_inspection.py`** except to fix a real defect: its `MutatingRequestBlocker` is the safety net for
  applying. Its inspection tooling (`inspect-apply`) is frozen.
- **Naukri's login page can flash its form before redirecting a logged-in session.** `login()` waits for a late redirect; keep that.
- **Telegram's connection drops now and then.** Waiting for a tap must survive a dropped connection (`TelegramChannel._wait`).
- **Statuses:** `ApplicationStatus` has NOT_APPLYING and IGNORED besides the original ones; both are excluded from digests by default.
- **Windows specifics:** use PowerShell for Task Scheduler; the shell tool is Git Bash. Avoid `rm -rf` with shell variables.

## Testing

Three tiers: unit; mocked-browser (`test_browser_*.py`, no real browser); manual (`pytest -m manual`, excluded by default). Run
`pytest` before calling a change done. Currently **1599 passed, 3 deselected**. A fix for a live bug gets a test built from the
real shape that failed (see `tests/test_choice_questions.py` for the Indium panel).

## Working style the user expects

- Explain in **plain, simple English**; give a recommendation, not a survey of options. Tables for comparisons are welcome.
- **Explain the design and trade-offs before building anything that changes behaviour**, and get a clear yes. Small bug fixes the
  user has reported can be fixed directly, then explained.
- **Commit and push only when the user says so** ("commit and push it"). Commit messages end with the `Co-Authored-By` line the
  session specifies. Never skip hooks or force-push.
- **Say plainly what is verified against the real site and what is a guess.** Report failures as failures.
- Do not click, apply, press or change anything on the user's Naukri account to test a theory; read-only page checks are fine.
- Prefer the smallest correct fix. Keep the tests green and say how many pass.
- When the user pastes an email or a screenshot, read it carefully: their real-world observations have repeatedly been right
  (an "Applied" marker that appeared, a question that was skipped, a login that flashed).
