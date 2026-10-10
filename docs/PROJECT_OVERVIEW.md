# naukri-agent — Project Overview & Study Guide

For anyone reading this codebase for the first time, or coming back after a break: **what the system does, why it is built
this way, and how the pieces fit**. It describes the system as of October 2026.

Companions: `README.md` (setup and running), `CLAUDE.md` (working rules and notes for a coding session), the module docstrings
(each explains *why* its module exists), and `docs/*.html` (the architecture and workflow diagrams).

---

## 1. What this project is

`naukri-agent` is a personal job-search assistant for Naukri.com. Every day it finds fresh jobs, reads them, scores them against
your profile with plain arithmetic, and tells you about the best ones. It can also **apply to Naukri Apply jobs for you, one job
at a time, only after you tap Yes on Telegram**. For jobs that apply on the employer's own site it gives you the direct link,
researches the company, and later asks you on Telegram whether you applied, so the history stays complete.

It started as a digest-only tool and has grown in stages. The rule that held through all of it: **the AI reads, plain code
decides, and a human approves every application.**

### A day in its life

| Time | What happens |
|---|---|
| 09:45 | **Profile refresh.** Uploads the next of your resumes to Naukri so the profile counts as freshly updated (rotates between them). |
| 10:00 | **Daily run.** Searches, opens and reads 50 fresh jobs, scores them, picks the top 10 scoring 80 or more, emails the digest, pings Telegram. |
| 10:05 | **Research.** Waits for the digest, then researches the company-website jobs in it and emails one research email. |
| 10:30, 15:00 | **Watchdog.** Silent unless the refresh, the run or the digest did not happen, then it alerts on Telegram. |
| 20:00 | **"Did you apply?"** Telegram asks about company-website jobs still waiting for an answer. Silent if none. |
| Sunday 22:00 | **Weekly report.** An Excel file of every job given that Monday to Sunday and its status, emailed to you. |
| Always on | **Phone control.** A small listener on the PC answers `/apply`, `/applied`, `/status`, `/help` from your Telegram chat. |

Each of these is a Windows scheduled task that wakes the PC if it is asleep. While a long job runs, the app asks Windows not to
sleep (`power.py`); the screen may still turn off.

---

## 2. Design principles

| Principle | Where it is enforced |
|---|---|
| **A human approves every application.** | `orchestration/auto_apply_runner.py`: each job is sent to Telegram with Yes/No; silence, No or a timeout means no. `/apply` only *starts* that run. |
| **The AI never decides.** | `matching/scorer.py` is pure arithmetic. The LLM (`jobs/parser.py`) only extracts fields from a posting. The research agent only reads and reports. |
| **The daily pipeline cannot apply.** | Structural tests fail if the pipeline, discovery, scheduler, research agent, phone listener or follow-up modules import any apply code. |
| **Never bypass CAPTCHA, MFA or anti-bot protection.** | `browser/login.py` raises on detection and pauses for a human. No stealth. A site that blocks a plain visitor is left alone. |
| **Never press "Apply on company site".** | Naukri marks the job Applied when it is pressed (seen 2026-10-09). The employer's address is read from the job data Naukri's own page already loads (`browser/company_link.py`). |
| **Job postings are untrusted text.** | The parser's prompt ignores embedded instructions, and its reply is validated against a whitelist schema. The research agent treats everything it reads as untrusted and its fetches are SSRF-guarded. |
| **The database is the truth; exports are copies.** | Excel files are rebuilt from the database. `ApplicationHistory` is the only record of whether you applied. |
| **No resume generation.** | `resume/` only selects among your own files. |
| **Selectors live in one file.** | `browser/selectors.py`, each marked VERIFIED (with date) or UNVERIFIED. |
| **A failing step never hides a finished application.** | After an error following the Apply click, Naukri's own "Applied" marker is checked before anything is called failed. |
| **Failures leave evidence.** | A failed apply saves a screenshot and the question panel's HTML under `inspection_output/auto_apply/`. |
| **Secrets stay in `.env`.** | Git-ignored with `config/*.yaml`, `resumes/`, `data/`, `logs/`, `out/`, `inspection_output/`. Credentials never appear in logs or messages; errors report only their type. |
| **Real email only when asked.** | `EMAIL_SENDER=smtp` needs all four SMTP settings or it fails loudly. |

---

## 3. Architecture map

```
src/naukri_agent/
├── config.py            All settings (validated); the only place that reads the environment
├── power.py             Keep Windows awake while a long job runs
├── candidate/ resume/   Your profile and resumes (YAML); selection among YOUR resume files
├── jobs/                Job models, JobParser (LLM extraction), skill evidence merge
├── matching/            The deterministic scorer (7 categories) and its sub-scorers
├── llm/                 Provider abstraction: Ollama (local), Groq, OpenAI
├── browser/             Playwright: login, search, job pages, profile upload, the apply panel
│                         selectors.py · company_link.py · apply_workflow.py · apply_inspection.py (network guard)
├── database/            SQLAlchemy models and repository functions (SQLite)
├── orchestration/       pipeline.py (daily run) · discovery.py · auto_apply_runner.py (telegram-apply)
│                         telegram_listener.py + followup.py (phone control) · profile_refresh.py · watchdog.py · apply_lock.py
├── recommendations/     build_digest: eligibility, ranking, Parts 1–3 · apply_ready.py · employment.py
├── research_agent/      The company-site researcher: a tool-using agent (read-only) + ATS detection
├── notifications/       Email (file / console / SMTP with attachments), Telegram, rendering
├── reporting/           excel.py (mirror) · weekly.py (Sunday report)
├── scheduler/           Optional in-process scheduler
├── agents/              profile_answers (answers from your profile, no AI), apply_answer_agent, grounding
├── legacy/              Older commands that still work but are not part of the daily flow
└── cli/                 `naukri-agent <command>` (25 commands)
```

### The daily flow

```
Naukri search (role-only searches first, then cities, stop at 50 fresh jobs) ─▶ open and read each job (read-only;
keeps the employer's direct link) ─▶ JobParser (LLM, extract only) ─▶ score_job() (plain code) ─▶ select_resume()
─▶ build_digest(): ≥80, ≤10, company-website jobs = Part 1, last day's apps = Part 2, recent company-site jobs = Part 3
─▶ email · Excel mirror · Telegram ping (up to 4 Naukri Apply jobs ready) ─▶ research email (10:05 task)
```

### The scorer

Seven categories add to 100: skills 35, experience 20, role 15, salary 11, location 7, education 5, posted recently 7 (7 marks if
posted today down to 1 at a week old). ACCEPT is 80 or more, REVIEW 70 or more. Missing data earns partial credit with an
explanation, never a silent zero. Only Tier 1–2 skill matching (exact and curated aliases, then compound normalisation) is used in
production: the LLM-based Tier 3 was built, tested against a real model, found to be confidently wrong in places, and left
unreachable on purpose (see `matching/semantic_skill_matcher.py`).

### The daily plan

At most 10 jobs a day: up to 4 through Telegram (Naukri Apply jobs, the daily cap) and the rest for you to apply to on the company
site. Only jobs scoring 80 or more are listed, so fewer is normal. Contract and temporary jobs are left out (Naukri's own
Employment Type line decides).

---

## 4. How applying works

**Naukri Apply jobs (through Telegram).** `/apply` (or `naukri-agent telegram-apply`) starts a run that, for each ready job:
sends a card (score, skills matched and missing, resume to use), waits for your Yes, puts the role-matched resume on your profile
if it is not already there (verified, or nothing is applied), clicks Apply, and answers any questions: from your profile when
there is one clear value, otherwise by asking you (choices as buttons). It counts as applied only when Naukri's own page shows
"Applied". A conservative stop follows any unconfirmed result. At most 4 applications per rolling 24 hours.

The apply panel is Naukri's chat widget. It is the least predictable part of the system: question types (radio buttons, text boxes,
checkboxes) and even the order of questions vary, and Naukri sometimes skips a question. The code handles each type seen so far and
reports a skipped question; a new type will show up as a clean stop with a saved snapshot.

**Company-website jobs.** Part 1 of the digest gives each job with its direct apply link. You apply yourself. At 20:00 or on
`/applied`, Telegram asks **Applied / Not applying / Later** for each job still waiting. Applied is recorded as *applied through
company website*; Not applying as *not applied*; the 4th Later in a row as *ignored*. The last two are never suggested again.
Part 3 of the digest and the weekly report show where each one stands.

**Labels** (`ApplicationHistory.apply_label`): *applied directly* (on Naukri, by the app or by you by hand), *applied through
company website*, *not applied*, *ignored*.

---

## 5. Records: three axes, never conflated

| Axis | What it is | Written by |
|---|---|---|
| Raw discovery | `Job`, `JobExtraction`, raw skill evidence | The scraper and the parser only |
| Recommendation history | `JobRecommendation`: one row per job per daily run | `build_digest()` only |
| Application history | `ApplicationHistory` (+ `ApplicationEvent`): the only authoritative record of what you did | `telegram-apply` after a confirmed application, the follow-up answers, and `mark-applied` / `mark-status` |

A recommendation existing never implies you applied. Reposts collapse to a canonical job id everywhere. Other tables:
`AutoApplyAttempt` (every apply try and outcome, which also drives the cap and retries), `ApplicationQuestion` (each question and
the answer sent), `FollowupPrompt` (how often you answered Later), `JobResearch`, `ResumeRefresh`, `DailyRun`, `RunEvent`.
New columns and tables are added automatically at startup (nullable columns only), so there is no migration tool.

---

## 6. The research agent

A small tool-using agent (Groq `openai/gpt-oss-120b`, with Serper or Brave search) for each company-website job in the digest: it
searches, reads public pages, and submits a report. **It cannot apply, log in, fill forms or press anything.** Code, not the model,
writes the sources, the "how you'll apply" line (from the careers page's application system, `research_agent/ats.py`) and the
direct link. A report may only cite addresses the agent actually saw. A daily token budget (about 10 jobs) keeps it inside the
free tier. Careers pages that show no text to an automated reader are flagged, and the email tells you what to search for.

---

## 7. Testing

Three tiers, kept separate: unit tests (pure logic), mocked-browser tests (`tests/browser_fakes.py`, no real browser or network),
and manual tests in `tests/manual/` that need a real Naukri account (`pytest -m manual`, excluded by default). Currently
**1599 passed, 3 deselected**. Run `pytest` before considering any change done. Apply-flow behaviour that only Naukri's real page can
confirm is called out as UNVERIFIED in `selectors.py` until a real run proves it.

---

## 8. Operating it

- **Scheduled tasks:** "Naukri Agent - Daily Jobs", "- Profile Refresh", "- Research", "- Watchdog", "- Phone Control", "- Weekly
  Report". They run only while you are signed in (locked is fine). Do not close the black window of the phone listener.
- **Backups:** copy `data/naukri_agent.db` before any manual edit to it.
- **When something fails:** `logs/naukri_agent.log`; `run_events` in the database (every stage); `inspection_output/auto_apply/*_failure.*`
  for a failed apply; `inspection_output/login_diagnostics/` for a login problem; the watchdog's Telegram alert.
- **Pause switches:** create `data/PAUSE_AUTO_APPLY` or `data/PAUSE_PROFILE_REFRESH` to stop either.
- **Legacy commands** (`apply`, `inspect-profile-edit`) live in `legacy/`; `inspect-apply` and `inspect` are one-off inspection tools.

---

## 9. Where to look next

- **A score looks wrong:** `matching/scorer.py` and the specific sub-scorer, then `jobs/skill_evidence.py` for mis-classified skills.
- **The digest is wrong:** `recommendations/builder.py` (eligibility and ranking) and `notifications/render.py` (the text).
- **The apply panel broke:** `browser/apply_workflow.py` and `browser/selectors.py`; read the saved failure snapshot first.
- **Login or the site changed:** `browser/login.py`, `browser/selectors.py`, and the login diagnostics folder.
- **Phone control or the questions:** `orchestration/telegram_listener.py`, `followup.py`, `notifications/telegram.py`.
