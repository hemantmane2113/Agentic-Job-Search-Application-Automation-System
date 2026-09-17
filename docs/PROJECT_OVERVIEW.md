# naukri-agent — Project Overview & Study Guide

This document is for anyone reading this codebase for the first time —
a new contributor, a reviewer, or the maintainer coming back after a
break — who wants to understand **what the system does, why it is
built the way it is, and how the pieces fit together**, down to the
level of specific functions and files.

It is a companion to, not a replacement for:
- `README.md` — setup/run instructions and the phase table.
- `CLAUDE.md` — live handoff notes, including the detailed, dated
  history of every real-Naukri bug fix and design decision. This
  document explains the *shape* of the finished system; `CLAUDE.md`
  explains *how it got there* and exactly what's still unverified.
- Module docstrings — each one explains *why* that module exists, not
  just what it does; read them before changing a module's behavior.

---

## 1. What this project is, and how its scope changed

`naukri-agent` discovers job listings on
[Naukri.com](https://www.naukri.com), scores them against a candidate
profile using a transparent, deterministic algorithm, and — this is
the part that changed mid-development — **emails a ranked daily
digest of the best matches**. It does not apply on your behalf.

**Objective change (2026-09-09).** The project originally set out to
also submit applications automatically, with a human approving every
write action. That goal was explicitly dropped partway through
development. The current, real objective is:

> "Every day at 10:00 AM, discover relevant Naukri jobs, read/
> understand their job descriptions, rank them against my
> CandidateProfile and MasterResume, and email me the best matching
> jobs with their Naukri links and concise reasons for the match."

Applying stays entirely manual — you click Apply yourself, on
naukri.com, and record that you did with `naukri-agent mark-applied`.
All of the apply-workflow reconnaissance code that was built before
this change (`browser/apply_inspection.py`, the `inspect-apply` CLI
command) is **explicitly frozen**: left in the tree, fully functional
as a read-only inspection tool, but never called from anything
automated and never extended toward actually submitting anything.

This history matters for reading the rest of the codebase: you'll see
two eras side by side — the original "Phase N" numbering (phases 1–9,
covering scaffolding through the abandoned apply-preparation phases)
and the post-objective-change "Stage A / Stage B" work (the daily
digest and its delivery/scheduling). Both are real, both are in the
tree; Stage A/B is what actually runs today.

## 2. Who should read this, and how

- **Want the mental model fast?** Read §3 (design principles) and §4
  (architecture map + data flow), then skip to §7 (end-to-end
  walkthrough).
- **About to touch a specific module?** Jump to its subsection in §6.
- **Setting the project up locally?** See `README.md`; this document
  doesn't repeat installation steps.

---

## 3. Design principles

| Principle | Where it's enforced |
|---|---|
| **Applying is entirely manual.** The system never submits anything. | `NaukriClient.prepare_application()` still raises `NotImplementedError`. `ApplicationHistory` rows are written *only* by the `mark-applied`/`mark-status` CLI — never by discovery, scoring, or an LLM. |
| **The apply-workflow reconnaissance tool is frozen, not deleted.** | `browser/apply_inspection.py` + `inspect-apply` are real, tested, and safe (see §6.8) — but reachable *only* via their own standalone CLI command, never from the daily pipeline. |
| **The LLM never makes the final decision.** Deterministic Python owns scoring, ranking, and eligibility. | `matching/scorer.py` is pure arithmetic. The LLM (`jobs/parser.py`) only extracts structured fields from a posting; `recommendations/builder.py`'s eligibility/ranking logic has no LLM call in it at all. |
| **A validated-but-risky LLM capability stays disabled until it's actually safe.** | `matching/semantic_skill_matcher.py`'s Tier 3 (LLM semantic skill matching) was built, then *empirically tested* against a real Ollama model, found to produce confident, well-formed, but simply wrong semantic judgments (see §6.6) — and is kept unreachable from the production scoring path as a result. This is the project's clearest example of "we built it, then didn't trust it just because it compiled." |
| **Job postings are untrusted, adversarial text.** | `jobs/parser.py`'s system prompt tells the model to ignore embedded instructions, *and* the response is validated against a whitelist Pydantic schema that silently drops anything not in the schema. |
| **No resume generation, ever.** | `resume/registry.py`/`resume/selector.py` only select among the user's own pre-existing files. |
| **The database is the single source of truth; every export is a disposable, regenerated mirror.** | `reporting/excel.py`'s `export_workbook` rebuilds the entire `.xlsx` from the DB on every call — hand-edits are never preserved. |
| **Missing data earns partial credit and an explanation, never a silent zero.** | `matching/salary_matcher.py`, `experience_matcher.py`. |
| **Never bypass CAPTCHA, MFA, or anti-bot protections.** | `browser/login.py` raises immediately on detection; never attempts to solve. |
| **No real email unless explicitly opted into.** | `EMAIL_SENDER` defaults to `file`; `smtp` requires all four SMTP settings or fails loudly (`EmailConfigError`) rather than silently falling back. |
| **A scheduled run failing once must never mean it stops running forever.** | `scheduler/daemon.py`'s `_run_job_safely` logs and swallows an exception rather than letting APScheduler cancel the job's future firings. |
| **Selectors live in exactly one file.** | `browser/selectors.py`. |
| **Skill normalization is a lookup table, never capability inference.** | `matching/skill_normalizer.py`'s alias table maps only genuine synonyms. |
| **Every score and every recommendation decision is explainable.** | `CategoryScore.positive_factors`/`negative_factors`; `recommendations/explain.py`'s `MatchExplanation`. |

---

## 4. Architecture map and data flow

```
src/naukri_agent/
├── config.py            Central validated Settings — the ONLY place that reads os.environ
├── logging_config.py     Rotating file + console logging (setup_logging(), called once at startup)
│
├── candidate/            CandidateProfile — job-search PREFERENCES (YAML-loaded)
├── resume/               MasterResume (factual record) + ResumeRegistry/Selector
├── jobs/                 Job domain models, JobParser (LLM extraction), skill_evidence.py
├── matching/             Deterministic JobScorer + semantic_skill_matcher.py (Tier 3 disabled)
├── llm/                  Provider-agnostic LLM abstraction (Ollama / Groq / OpenAI)
├── browser/              Playwright automation — read-only discovery/detail-fetch (live);
│                          apply_inspection.py (Stage 1.5, FROZEN, read-only)
├── database/             SQLAlchemy ORM models + repository (upsert) functions
├── orchestration/        discovery.py (read-only discover+store) + pipeline.py
│                          (run_daily_recommendations — the daily business logic)
├── recommendations/      build_digest: rank/filter JobMatch rows into an emailable digest
├── notifications/        FileEmailSender / ConsoleEmailSender (default) + SmtpEmailSender (opt-in)
├── reporting/            excel.py — 3-sheet workbook regenerated from the DB every run
├── scheduler/            Stage B: naukri-agent scheduler — an in-process daily trigger
├── cli/                  `naukri-agent <command>` entrypoint
└── agents/               (empty placeholder — unused)
```

### Data flow at a glance

```
   Naukri.com (read-only: search + job-detail pages)
       │
       ▼
  discover_and_store()  ──upsert_job──▶  Job (DB, dedup + repost detection)
       │  (freshness-first gate: only recent, dated postings go on to parse)
       ▼
  JobParser (LLM, untrusted-text defenses) ──▶ JobExtraction (versioned)
       │                                            │
       │                    merge with raw JD/Key-Skills-DOM evidence
       │                    (jobs/skill_evidence.py, Tier 1-4 claim-strength)
       ▼                                            │
  score_job() — 100% deterministic ◀────────────────┘
       │
       ▼
  JobMatch (DB)  +  select_resume() ──▶ ResumeSelection (DB)
       │
       ▼
  build_digest() — eligibility (application/cooldown-aware) + ranking
       │                                    │
       ▼                                    ▼
  RecommendationDigest            JobRecommendation (DB, one row per email)
       │
       ▼
  render_digest() ──▶ send via FileEmailSender / ConsoleEmailSender / SmtpEmailSender
       │
       ▼
  export_workbook() — regenerates job_search_history.xlsx from the DB
       │
       ▼
  DailyRun + RunEvent audit trail (every stage logs a structured event)

  Triggered by: naukri-agent run-daily (one-shot) OR naukri-agent scheduler
  (in-process, daily) OR an OS-level cron / Task Scheduler entry calling run-daily.

  ─── separate, frozen, never on the path above ───
  naukri-agent inspect-apply → browser/apply_inspection.py:
  a human-driven, read-only inspection of Naukri's post-Apply UI.
  Automation never clicks Apply; a default-deny network guard blocks
  every mutating request except one narrowly allowlisted, metadata-only
  exception needed to render the UI.
```

---

## 5. Raw vs. derived vs. authoritative — three axes, never conflated

Stage A introduced a third axis on top of the original raw/derived
split (§5 in the original design), and the project is explicit that
all three must stay separate:

| Axis | What it is | Written by |
|---|---|---|
| **Raw discovery** | `Job`, `JobExtraction`, `JobRawSkillEvidence` | The scraper / LLM parser only |
| **Recommendation history** | `JobRecommendation` — one row per job per daily run, records what was emailed and when | `build_digest()` only |
| **Application history** | `ApplicationHistory` (+ `ApplicationEvent` audit log) — the *only* authoritative record of whether the user actually applied | The manual `mark-applied` / `mark-status` CLI only |

A `JobRecommendation` existing **never** implies the user applied — a
job can be recommended many times if never applied to. This is why
`build_digest`'s eligibility logic (§6.5) checks `ApplicationHistory`
and `JobRecommendation` as two independent signals with different
consequences (permanent exclusion vs. a cooldown).

---

## 6. Module-by-module walkthrough

### 6.1–6.5 Config, candidate/resume, matching engine, LLM abstraction, database layer

These are architecturally unchanged since the original design and are
covered in full in `CLAUDE.md`'s "Architecture quick reference" and
inline module docstrings — read those for `config.py`,
`candidate/models.py`, `resume/*.py`, `matching/scorer.py` and its
sub-scorers, and `llm/*`. Two things worth calling out that *are* new:

**`matching/skill_matcher.py`/`skill_normalizer.py` now sit behind a
3-tier hybrid (`matching/semantic_skill_matcher.py`)** — but only
Tiers 1–2 are ever exercised in production:
- Tier 1 (unchanged): exact match + the hand-curated alias table.
- Tier 2 (new, deterministic, zero LLM cost): compound/qualifier
  normalization — no model call.
- Tier 3 (LLM semantic matching): **built, validated, and disabled.**
  Real-Ollama testing found it confidently misclassified pairs like
  "Redis" as an abbreviation of "MongoDB" — well-formed, schema-valid,
  but simply wrong. Since a false positive here is worse than a false
  negative (it would credit a skill the candidate doesn't have), Tier
  3 stays unreachable from `matching/scorer.py`'s call path until a
  fundamentally different verification approach exists. Read
  `resolve_skills_for_job()`'s docstring for exactly how it's kept
  unreachable — this isn't a TODO, it's a load-bearing safety decision
  that happens to look like unused code if you don't read the comment.

**`jobs/skill_evidence.py`** merges up to four independent inputs
(Naukri's ld+json `skills`, its Key Skills DOM chips, the LLM's
required/preferred lists, and deterministic vocabulary recovery
against the full JD text) into one record per skill, via a 4-tier
**claim-strength** hierarchy — not "source X always wins," but "how
strongly did *this specific mention* establish requiredness." Read the
module docstring; it's short, precise, and worth it verbatim before
touching scoring inputs.

### 6.6 `browser/` — read-only discovery is live; the apply workflow is frozen

```
browser/
├── browser_manager.py       Owns the Playwright lifecycle exclusively
├── selectors.py               The ONLY file with raw CSS/XPath strings
├── login.py                     Fills the form, classifies CAPTCHA/MFA/success
├── jobs.py                        search_jobs() + fetch_job_detail() — both read-only
├── naukri_client.py                 Facade; prepare_application() still NotImplementedError
├── inspection.py                      Stage 1: `naukri-agent inspect`
└── apply_inspection.py                  Stage 1.5: `naukri-agent inspect-apply` — FROZEN
```

`fetch_job_detail()` is the piece Stage A actually depends on: a plain
`page.goto()` + text extraction from a job's public detail page,
feeding `orchestration/discovery.py`. It never clicks or fills
anything.

`apply_inspection.py` deserves its own read even though it's frozen,
because it's the most carefully safety-engineered file in the
codebase:
- The automation **never clicks Apply** — a human does, in a visible
  browser window.
- A `MutatingRequestBlocker` installed on the browser context
  **default-denies every mutating request** (POST/PUT/PATCH/DELETE)
  for the rest of the session, with exactly **one** narrow,
  exact-path-matched exception (the apply-initialization request
  needed just to render the UI) — and even that exception only records
  sanitized response *metadata* (status, media type, a shape
  fingerprint of key names/types), never the actual body.
- Secrets are structurally excluded from anything it logs: request
  headers are never read at all; only method + URL path + top-level
  key *names* are recorded.
- CAPTCHA/MFA handling is reused unchanged from Stage 1 — pause for a
  human, never attempt to solve.

If you're evaluating whether this project is safe to run, this file
(and its very long, dated bug-fix history in `CLAUDE.md`) is the one
to actually read in full rather than take on faith.

### 6.7 `orchestration/` — the daily pipeline's business logic

**`discovery.py`**`.discover_and_store()`: builds a query matrix from
the candidate's preferred roles × locations (or an explicit
`discovery_queries` override), searches, dedups by URL, then applies a
**freshness-first gate** — `_parse_card_age_days()` deterministically
parses Naukri's relative posted-date labels ("3 days ago", "Just now",
"30+ Days Ago") into an age, excludes anything older than
`discovery_freshness_days` *or with an unparseable date* (absence is
never assumed to mean fresh), sorts newest-first, and only fetches
full detail for `discovery_fresh_job_limit` of them. This exists
specifically so a 200-job search doesn't require 200 LLM calls before
you find out only the top 10 matter.

**`pipeline.py`**`.run_daily_recommendations()` is the actual daily
business logic (no scheduling in this file — see §6.10):

```
load profile/resume/registry → upsert candidate → DailyRun(STARTED)
  → discovery (read-only)
  → per job: LLM parse (optional) + merge skill evidence
             + score_job() (deterministic) + select_resume() (static)
  → build_digest() (application/cooldown-aware, capped, ranked)
  → render_digest() → send (file/console by default; smtp only if
     EMAIL_SENDER=smtp is explicitly configured)
  → export_workbook() (regenerated from DB; a failure here never
     fails the run)
  → DailyRun(COMPLETED/FAILED)
```

Every stage writes a short, non-sensitive `RunEvent` — this is what
makes a scheduled run's failure mode diagnosable without re-running it
(`naukri-agent doctor`, `logs/naukri_agent.log`, or querying
`RunEvent` directly all show the same audit trail).

### 6.8 `recommendations/` — eligibility and ranking, fully deterministic

`build_digest()` (`recommendations/builder.py`) is worth reading end
to end — it's the piece that actually decides what you see in your
inbox, and every rule in it is auditable:

**Eligibility**, in order:
1. `ApplicationHistory.status` in `recommendation_exclude_if_status`
   (default: APPLIED/INTERVIEW/OFFER/REJECTED/WITHDRAWN) → excluded
   **regardless of cooldown** — once you've acted on a job, it stops
   coming back.
2. Previously recommended but not applied → gated by
   `recommendation_cooldown_days` (`0` = eligible again next run; `>0`
   = only after that many days).
3. Never recommended → eligible.
4. A parse failure *this run* (LLM extraction failed, so the score is
   raw-listing-only and can only be inflated) → excluded from
   recommendation eligibility even though its `JobMatch` row still
   exists for diagnostics.
5. Reposts collapse to the **canonical job id** everywhere (via
   `canonical_job_id()`), so a repost of an applied job correctly
   inherits that job's application history rather than looking new.

**Ranking** is bucketed, not a flat score sort: freshness bucket
(0–1 / 2–3 / 4–7 / older-or-unknown days since posting) is the
*primary* key, `overall_score` descending is secondary within a
bucket, then posted recency, then `first_seen_at` as a final
deterministic tie-break. This means a slightly-lower-scoring, more
recently posted job can outrank an older higher-scoring one — a
deliberate product decision (freshness matters for a daily digest),
not a scoring bug.

`recommendations/explain.py`'s `explain_match()` produces the
human-readable reasons/gaps directly from `MatchResult`'s deterministic
factors; an LLM narrative is layered on only if
`explanation_use_llm=true`, and is dropped if it would reference
application status (which must stay purely factual/DB-sourced).

### 6.9 `notifications/` — delivery, with a real SMTP option

```python
def build_email_sender(settings: Settings) -> EmailSender:
    choice = (settings.email_sender or "file").strip().lower()
    if choice == "console":
        return ConsoleEmailSender()
    if choice == "smtp":
        missing = [name for name, value in (...) if not value]
        if missing:
            raise EmailConfigError(...)  # fails loudly, never silently falls back
        return SmtpEmailSender(...)
    return FileEmailSender(settings.email_output_dir)
```

`FileEmailSender` (the default) writes a timestamped `.txt` (and
`.html`, if present) under `email_output_dir` — nothing is sent
anywhere. `SmtpEmailSender` sends real mail via stdlib `smtplib` +
STARTTLS, works with Gmail (App Password) or any standard STARTTLS
provider, and is opt-in only: `email_sender` defaults to `file`, and
switching to `smtp` requires *all four* of `SMTP_HOST`/
`SMTP_USERNAME`/`SMTP_PASSWORD`/`NOTIFY_EMAIL_TO`. Every sender's
failure is reported by exception *type* only (`type(exc).__name__`),
never the message — some SMTP server error responses can echo back
parts of the failed request, and this project treats that as a
potential leak vector worth designing around rather than a
theoretical concern.

### 6.10 `scheduler/` — an in-process alternative to OS-level cron

`naukri-agent scheduler` is genuinely optional — an OS-level cron
entry or Windows Task Scheduler task calling `naukri-agent run-daily`
directly works exactly as well and is fully supported (see README's
"Scheduler" section for the tradeoffs). `scheduler/daemon.py` exists
for whoever would rather not depend on OS-level infrastructure:

```python
def build_scheduler(settings=None, job_fn=run_daily_recommendations) -> BlockingScheduler:
    settings = settings or get_settings()
    hour, minute = _parse_daily_run_time(settings.daily_run_time)
    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(_run_job_safely, trigger=CronTrigger(hour=hour, minute=minute, ...),
                       args=[settings, job_fn], id=JOB_ID, misfire_grace_time=3600, coalesce=True)
    return scheduler
```

`build_scheduler()` is deliberately separate from `run_scheduler()` so
tests can inspect the configured job/trigger without ever calling the
blocking `.start()`. `_run_job_safely()` is the piece that keeps one
bad day from becoming a permanently-stopped scheduler — APScheduler
cancels a job's *future* firings by default if it raises, so this
wrapper logs the exception type and swallows it instead.

### 6.11 `reporting/` — a disposable, regenerated mirror

`export_workbook()` rebuilds `job_search_history.xlsx` (Jobs /
Applications / Daily Runs sheets) from the database on every call.
Nothing about it is incremental or hand-editable-and-preserved — the
database is the only source of truth, by design (§3).

### 6.12 `cli/main.py` — the command surface

`doctor` remains the fully-implemented, always-safe-to-run diagnostic
(Python version, config load, LLM provider credentials, DB
connectivity, candidate/resume/registry file checks, and now
"Digest tables reachable"). Real, working commands: `run-daily`,
`discover`, `recommend`, `export-excel`, `mark-applied`, `mark-status`,
`applications`, `report`, `scheduler`, `inspect`, `inspect-apply`.
`prepare`/`apply`/`match` are explicit, permanent `NotImplementedError`
stubs — not "not built yet," but "out of scope by design" for the
first two.

`main()` (the actual console-script entry point) wraps `get_settings()`
in a try/except that prints a clean, actionable message and exits 1
instead of showing a raw Pydantic traceback if `.env` has a bad value
— logging isn't configured yet at that point in startup, so there's
nowhere for a "nicer" version of the traceback to go either; the fix
is to never show one.

---

## 7. End-to-end walkthrough: one daily run, start to finish

1. **Trigger** — `naukri-agent run-daily` (one-shot), `naukri-agent
   scheduler` (in-process, daily), or an OS-level cron/Task Scheduler
   entry calling `run-daily`. All three call the exact same
   `run_daily_recommendations()`.
2. **Discovery** — `orchestration.discovery.discover_and_store()`:
   read-only search across the candidate's role×location matrix,
   dedup, freshness-first filtering, then a read-only detail fetch per
   surviving job, upserted into `Job` (+ raw skill evidence).
3. **Understanding** — `JobParser` (LLM, untrusted-text defenses) →
   `JobExtractionCreate`, then merged with raw JD/Key-Skills-DOM
   evidence via `skill_evidence.build_skill_evidence()` before being
   persisted as `JobExtraction`.
4. **Scoring** — `matching.scorer.score_job()`, 100% deterministic,
   persisted as `JobMatch`.
5. **Resume selection** — `resume.selector.select_resume()`, persisted
   as `ResumeSelection`.
6. **Digest** — `recommendations.builder.build_digest()`: eligibility
   (application/cooldown-aware) → bucketed ranking → cap → one
   `JobRecommendation` row per kept item.
7. **Delivery** — `notifications.render.render_digest()` then
   `FileEmailSender`/`ConsoleEmailSender`/`SmtpEmailSender`.
8. **Reporting** — `reporting.excel.export_workbook()` regenerates the
   Excel mirror from the DB.
9. **Audit** — every stage above writes a `RunEvent`; the run itself
   finishes as a `DailyRun(COMPLETED|FAILED)` row.
10. **You** review the email, apply on Naukri yourself if interested,
    and run `naukri-agent mark-applied <job>` — the *only* thing that
    ever writes `ApplicationHistory`, which future runs then respect.

Separately, and never touching any of the above: `naukri-agent
inspect-apply` for a human-driven, read-only look at what Naukri's
post-Apply UI actually contains, should Stage 2 (real automated
applying) ever be reconsidered and re-approved.

---

## 8. Testing strategy

Same three-tier convention as before, now much larger:
- **Unit** — pure logic (matching, models, config, recommendations
  eligibility/ranking, skill evidence merge policy).
- **Mocked browser** (`test_browser_*.py`, `tests/browser_fakes.py`) —
  zero real network/browser dependency. Includes extensive coverage of
  `apply_inspection.py`'s network-blocking/allowlist/observer behavior
  against fake Playwright objects.
- **Manual/real-integration** (`tests/manual/`) — require a real
  account + Playwright browsers; excluded by default, run with
  `pytest -m manual`.

Current state: **855 passed, 3 deselected**, ~90% line coverage
project-wide. `logging_config.py` and the CLI's startup error-handling
path (`main()`, the `scheduler` command) are covered by
`tests/test_logging_config.py` and `tests/test_cli_startup.py`
respectively — both were genuine coverage gaps found and closed during
a testing/logging/error-handling review pass, not aspirational
placeholders.

---

## 9. Phase / stage status

| Phase or Stage | Status |
|---|---|
| 1–6 (scaffolding → resume selection) | ✅ done |
| 7 Stage 1 (read-only inspection) | ✅ done |
| 7 Stage 1.5 (`inspect-apply`) | ❄️ frozen — implemented, safe, deliberately not extended |
| 7 Stage 2 / 8 / 9 (write ops, application prep, approval interface) | ⛔ abandoned — application submission is explicitly out of scope |
| A (daily match digest) | ✅ done |
| B (real SMTP + scheduler) | ✅ done |
| 12 (testing, logging, error handling, docs polish) | ✅ this document, plus the logging/CLI-error-handling test gaps closed alongside it |

---

## 10. Where to look next

- **Tuning what gets recommended?** `recommendations/builder.py`'s
  eligibility rules and `config.py`'s `recommendation_*`/
  `daily_recommendation_limit`/`freshness_new_days` settings — probably
  before writing new logic.
- **Something wrong with a score?** `matching/scorer.py` → the
  specific sub-scorer, then `jobs/skill_evidence.py` if it's a skill
  that seems mis-classified as required/preferred.
- **Email not arriving / arriving wrong?** `notifications/email.py`
  (delivery) vs. `notifications/render.py` (content) — they're
  deliberately separate.
- **Broke against the real Naukri site?** `browser/selectors.py` —
  run `naukri-agent inspect` (or `inspect-apply` for the post-Apply
  UI), compare the saved capture, update only that file.
- **Considering re-approving automated applying?** Read
  `browser/apply_inspection.py` and its entire dated history in
  `CLAUDE.md` first — it documents exactly what's confirmed about
  Naukri's apply flow and what safety machinery already exists.
