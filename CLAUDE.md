# naukri-agent — project context for Claude Code

This file is read automatically by Claude Code at the start of every
session. It exists so work can continue here with full context,
picking up exactly where a prior chat-based session (claude.ai) left
off — read this before making any changes.

## What this is

An agentic job-search assistant for Naukri.com: discovers jobs, scores
them deterministically against a candidate profile, selects an
existing (never generated) resume file per application, and emails the
user a ranked daily digest of the best matches with their Naukri links
and concise reasons. Built from a detailed master-prompt spec,
developed in strict incremental phases with explicit approval required
between phases.

### Objective change (2026-09-09) — READ THIS FIRST

The project objective was changed by the user mid-development. It is
now: **"Every day at 10:00 AM, discover relevant Naukri jobs,
read/understand their job descriptions, rank them against my
CandidateProfile and MasterResume, and email me the best matching jobs
with their Naukri links and concise reasons for the match."**

- **Automatic application submission is abandoned.** All Apply-workflow
  reverse-engineering (Stage 1.5 `inspect-apply`, `apply_inspection.py`,
  `MutatingRequestBlocker`, `PostInitClientObserver`, the apply-init
  allowlist, `prepare_application()`) is **FROZEN** — left in the tree,
  untouched, not extended. Final application stays **manual**: the user
  applies on Naukri themselves and records it with `naukri-agent
  mark-applied`.
- The daily digest is implemented as **Stage A** (see its section
  below). Real SMTP email, any scheduler, and Stage 2 write-ops are
  explicitly **not** in Stage A.

## Ground rules that must never be violated

- **Never bypass CAPTCHA, MFA, rate limits, or anti-bot protections.**
  Detect and pause for human intervention; never attempt to solve.
- **`DRY_RUN=true` stays the default.** No code path should submit an
  application without this being deliberately overridden.
- **`prepare_application()` on `NaukriClient` stays `NotImplementedError`.**
  Application submission is out of scope (objective change). The daily
  digest never clicks Apply, fills a form, or submits anything.
- **Real email only ever goes out when `EMAIL_SENDER=smtp` is
  explicitly set (Stage B).** The default remains `file` (rendered
  under `email_output_dir`; `console` prints it instead). Unlike
  Stage A's original placeholder behavior, `email_sender="smtp"` no
  longer falls back to file — `SmtpEmailSender` (`notifications/
  email.py`) sends a real message via `smtplib`/STARTTLS, and fails
  loudly (`EmailConfigError`) rather than silently if any of
  `SMTP_HOST`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`NOTIFY_EMAIL_TO` is
  missing. Credentials are read only from `Settings`, never logged;
  failures report only the exception type, never its message.
- **DB is the source of truth; Excel is a regenerated read-only
  mirror.** `ApplicationHistory` is the *only* authoritative record of
  whether the user applied — never inferred, never asked of the LLM.
  The Naukri URL always comes from the `Job` DB row.
- **No resume generation.** `ResumeRegistry`/`ResumeSelector` only
  select among the user's own, pre-existing resume files
  (`resumes/*.pdf`/`.docx`). Nothing writes or rewords resume content.
  MasterResume is factual record + matching input only.
- **LLM never makes the final accept/reject/apply decision.**
  Deterministic Python (`matching/scorer.py`) owns that; the LLM
  (Phase 5's `JobParser`) only extracts/structures data from job
  postings, treated as untrusted text (prompt-injection defenses in
  `jobs/parser.py`'s system prompt + Pydantic whitelist schema).
- **`browser/selectors.py` is the ONLY file allowed to contain a raw
  CSS/XPath selector.** Every other module in `browser/` refers to a
  selector by name from there. Most selectors are still `UNVERIFIED`
  placeholders — see that file's docstring for which six are
  `VERIFIED` (confirmed against a real Stage 1 inspection run) and
  which still need verification via `naukri-agent inspect`.
- **Skill normalization is a small, explicit alias table only**
  (`matching/skill_normalizer.py`) — never capability inference (e.g.
  "Python" must never imply "Django").

## Architecture quick reference

```
candidate/    CandidateProfile (matching prefs) — YAML-loaded
resume/       MasterResume (factual record) + ResumeRegistry/Selector
jobs/         Job (raw) / JobExtraction (LLM-derived) / JobParser
matching/     Deterministic JobScorer (skills/experience/salary/role/location/education)
llm/          LLMProvider abstraction: OpenAIProvider/GroqProvider/OllamaProvider,
              provider+model configured independently, per-task model override supported
browser/      Playwright Naukri automation — Stage 1 (read-only) is done;
              read-only job-detail fetch (fetch_job_detail) added for Stage A;
              Apply-workflow (Stage 1.5 / Stage 2 write ops) is FROZEN
database/     SQLAlchemy models + repositories (upsert-pattern throughout)
              + Stage A tables: ApplicationHistory / ApplicationEvent /
              JobRecommendation / RunEvent (new-tables-only, no Alembic)
recommendations/  build_digest: rank deterministic JobMatch rows, apply
              application-aware + cooldown eligibility, cap, explain
notifications/    render_digest + FileEmailSender/ConsoleEmailSender (default)
              + SmtpEmailSender (opt-in via EMAIL_SENDER=smtp, Stage B)
reporting/    excel.py: export_workbook — 3-sheet workbook regenerated from DB
orchestration/    discovery.py (read-only discover+store) + pipeline.py
              (run_daily_recommendations — the daily business logic, no scheduling)
scheduler/    Stage B: daemon.py — naukri-agent scheduler, an in-process
              alternative to OS cron/Task Scheduler calling run-daily directly
cli/          `naukri-agent <command>` — see `doctor`, `run-daily`,
              `discover`, `recommend`, `export-excel`, `mark-applied`,
              `mark-status`, `applications`, `report`, `scheduler`
```

Full narrative design rationale for each phase is in `README.md`'s
"Development phases" table and inline module docstrings — those
docstrings are written to explain *why*, not just *what*, so read them
before changing a module's behavior.

## Phase status (as of this handoff)

| Phase | Status |
|---|---|
| 1 — scaffolding | ✅ approved |
| 2 — CandidateProfile + MasterResume | ✅ approved |
| 3 — Job raw/derived separation, dedup | ✅ approved |
| 4 — deterministic JobScorer | ✅ approved |
| 5 — LLM JobParser + provider abstraction | ✅ approved |
| 6 (revised) — ResumeRegistry + selection | ✅ approved |
| 7 Stage 1 — read-only Naukri inspection | ✅ approved, including a login-state bugfix (see below) |
| 7 Stage 1.5 — controlled post-Apply inspection (`inspect-apply`) | ❄️ FROZEN by the objective change. Code stays in the tree untouched; not extended. |
| 7 Stage 2 — write operations (resume refresh, apply prep) | ⛔ ABANDONED — application submission is out of scope. |
| A — daily match digest (objective change) | ✅ IMPLEMENTED 2026-09-09. read-only discovery + JD fetch, deterministic ranking, application/cooldown-aware filtering, ApplicationHistory + manual `mark-applied`, file/console digest, 3-sheet Excel mirror, `RunEvent` audit. |
| B — real SMTP + scheduler | ✅ IMPLEMENTED. `SmtpEmailSender` (opt-in via `EMAIL_SENDER=smtp`, fails loudly if misconfigured rather than silently falling back) and `naukri-agent scheduler` (`scheduler/daemon.py`, APScheduler `BlockingScheduler`, fires daily at `DAILY_RUN_TIME` in `TIMEZONE`, a bad day's exception is logged and swallowed rather than cancelling tomorrow's firing). OS-level cron/Task Scheduler calling `run-daily` directly remains a fully supported alternative to the in-process scheduler — see README's "Scheduler" section. 847 passed, 3 deselected. Not yet run live with `EMAIL_SENDER=smtp` or under the in-process scheduler against real Naukri. |

### Stage 1 history worth knowing

Two real-Naukri inspection runs happened, and two rounds of fixes came
out of them:

1. First real run found 6 selectors were wrong (guessed, never
   verified) — fixed in `selectors.py`, each now marked `VERIFIED`
   with the date and source capture file cited in a comment.
2. Second real run hit a genuine bug: `login()` unconditionally tried
   to fill the login form even when the persistent browser session
   was already authenticated (Naukri's login URL auto-redirects an
   authenticated session, and the code didn't check for that first) —
   this produced a raw, unhandled `Page.fill` Playwright timeout.
   Fixed with: an `is_authenticated()` guard checked both before and
   after `goto(LOGIN_URL)`, a settle-then-recheck step after manual
   CAPTCHA/MFA completion, and a narrow
   `except playwright.sync_api.Error` boundary in
   `inspection.run_inspection()` that converts Playwright interaction
   failures into the same structured JSON report — deliberately
   narrow, so a genuine programming bug (`TypeError`, etc.) still
   propagates instead of being swallowed.

**Captured (3rd real run, `inspection_output/20260909_023839`):** a
real job-listing page. `APPLY_BUTTON` presence is now confirmed
(`<button id="apply-button" …>` — appears twice, duplicate id: header
card + sticky header); its *click behaviour* is still unknown (JS-bound,
no href). The pre-click DOM has no application form / resume selector /
questions / submit — the post-Apply UI is injected dynamically into an
(initially empty) `<div id="chatbot-container">`.

### Stage 1.5 — `inspect-apply` (implemented 2026-09-09, not yet run)

`browser/apply_inspection.py` + `naukri-agent inspect-apply`. A
controlled, human-driven read of the post-Apply UI:
- launches with an **isolated, disposable** browser profile (under
  `inspection_output/_apply_session_profiles/`) unless `--reuse-session`;
- installs `MutatingRequestBlocker` on the browser context — a
  **fail-safe default-deny** guard: every non-`GET`/`HEAD`/`OPTIONS`
  request is aborted once armed. No endpoint-pattern guessing.
- guard is **armed right after login** (login itself is a POST, so it
  can't be armed earlier) and **never disarmed**; login-phase mutating
  requests pass through but are logged;
- **automation never clicks Apply or any application control** — the
  human clicks, then presses Enter; the tool only reads
  (`extract_application_ui`): full HTML, screenshot, the application
  root's `outerHTML`, and an enumeration of inputs/labels/buttons with
  resume-control / final-submit classification;
- logs are **secret-free**: request method + path + query/body key
  *names* only; request headers are never read.
- CAPTCHA/MFA path is Stage 1's, reused unchanged.

`RESUME_SELECTION_CONTROLS` stays `UNVERIFIED` (proven absent pre-click);
`APPLICATION_ROOT_CANDIDATES` in `selectors.py` are the Stage 1.5 read
targets, all `UNVERIFIED` until a real `inspect-apply` run.

**Reliability fix (2026-09-09, post first real run).** The first two
`inspect-apply` runs produced only `01_pre_apply.*` — no `report.json`.
Cause: `report.json` was written *after* the `with BrowserManager` block
and reached only on the success / `NaukriAutomationError` / `PlaywrightError`
paths, so `EOFError` from `input()` on a non-interactive stdin (or a
Ctrl-C) escaped and skipped it. Fix (safety model untouched):
`run_apply_inspection` now writes a **partial `report.json` before the
manual pause** and a **guaranteed one in a `finally`**; `EOFError` →
clear `NaukriAutomationError` ("run in an interactive terminal");
`KeyboardInterrupt` is recorded + persisted then re-raised; extraction
is wrapped (`error_type: "ExtractionError"`, artifacts still written);
post-Apply waits are bounded (`set_default_timeout(15000)` +
`wait_for_load_state(..., timeout=5000)`); requests the guard aborts
during the manual window are surfaced as a report note. The guard is
still context-level `route("**/*")`, armed before navigation, no
`disarm`/`unroute`. `inspect-apply` must be run in a real interactive
terminal.

**Login-phase timeout fix (2026-09-09, 2nd `inspect-apply` run).** The
first isolated-profile run reached the real login form (fresh profile,
so no persistent-session short-circuit), the POST to
`/central-login-services/v1/login` succeeded and the browser redirected
to `/mnjuser/homepage` — but `login()` then sat on
`page.wait_for_load_state("networkidle")` (line 97), which never
resolves on Naukri's authenticated SPA (background analytics/poll
connections keep the network non-idle), until its 30s Playwright
timeout → `TimeoutError`, raised *before* the `is_authenticated()` /
`LOGIN_SUCCESS_URL_FRAGMENT` checks just below it could run. Those
checks were fine; they were simply unreachable. The persistent-profile
Stage 1 runs never hit this because `login()` returns at the first
`is_authenticated()` check, well before the submit. Fix (login.py
only): replaced the `networkidle` wait with `_settle_after_login_submit()`
— bounded (`_POST_SUBMIT_SETTLE_TIMEOUT_MS = 15000`), non-fatal
`domcontentloaded` + `load` waits, both reliable on the SPA — then the
existing CAPTCHA/MFA/`is_authenticated()`/URL-fragment classification
runs as before. No new selectors, no changes to `selectors.py`,
`apply_inspection.py`, the mutation blocker, CAPTCHA/MFA handling, resume
logic, or `prepare_application()`. NOTE: `jobs.py:inspect_application_workflow`
still calls `wait_for_load_state("networkidle")` for the job page — it
has worked in 3 real Stage 1 runs (content pages do reach idle), so it
was left as-is, but it is the same latent pattern.

**CAPTCHA-lifecycle cleanup fix (2026-09-09, 3rd `inspect-apply` run).**
Naukri showed a CAPTCHA before submit; the tool correctly paused with
the browser open. The user then abandoned it (window closed) → the
Playwright connection died, and the run crashed in
`BrowserManager.__exit__` with a *masked* "Connection closed while
reading from the driver". Cascade: `login.check_already_logged_in`'s
`getattr(page, "url", "")` and `inspection._record_failure`'s
`getattr(page, "url", None)` don't survive a *raising* `url` property
(getattr's default only covers `AttributeError`), so the failure
propagated into `close()`, whose unguarded `_context.close()` raised a
second "Connection closed" that replaced the original. Fix (three
spots, nothing else): `BrowserManager.close()` clears state first
(idempotent) then does `_context.close()` / `_playwright.stop()` each
in its own swallow-and-log `try/except` — it can never raise, so
`__exit__` never masks; `inspection._safe_page_url()` and
`login._safe_current_url()` read `page.url` defensively (return `None`
if it raises). Result: a dead connection now ends the run with a
clean structured `report.json` (`completed: false`, the connection
loss recorded) and no secondary crash. Browser-stays-open-during-the-
wait and Enter→recheck→resume are unchanged; the mutation blocker,
apply-inspection logic, selectors, resume logic, and
`prepare_application()` are untouched.

**Apply-init allowlist (2026-09-09, 3rd `inspect-apply` run's report).**
The real run confirmed that clicking Apply fires exactly
`POST /cloudgateway-workflow/workflow-services/apply-workflow/v1/apply`,
which the default-deny guard blocked → no application UI rendered.
`MutatingRequestBlocker` now takes an optional `allow_exact` set of
`(METHOD, exact-url-path)` pairs; `run_apply_inspection(allow_apply_init=True,
default)` passes the single entry
`{("POST", "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply")}`.
A mutating request is let through ONLY when armed AND
`(method.upper(), urlsplit(url).path)` is an EXACT member — no
substring/prefix/regex, host ignored (the path is all the evidence we
have). For the allowed request the response METADATA is captured into
`report["network_blocker"]["apply_init_responses"]` (status, media
type, body size, top-level JSON key NAMES) via `route.fetch()` →
inspect → `route.fulfill(response=…)`; the body is never stored. Fetch
failure still allows the request (metadata note only). Everything else
— other paths, other methods (incl. PUT/PATCH/DELETE on the same
path), any submit/confirm endpoint — stays default-denied and
redacted-logged. `extract_application_ui` also now records
`frames`/`dialogs`/`question_field_count` (generic HTML/ARIA probes in
`selectors.py`: `FRAME_SELECTOR`, `DIALOG_SELECTOR_CANDIDATES`). No
auto-click, no submit, `prepare_application()` still
`NotImplementedError`, `DRY_RUN` default still `True`.

**Apply-init response shape (2026-09-09, after the live capture).** The
real allowed POST returned HTTP 200 `application/json`, 5994 bytes,
twice, top-level keys `applyRedirectUrl / aurusFlow / chatbotResponse /
flowType / jobs / ncFlow / pzero / skippableQuestions / statusCode`; no
DOM/iframe/dialog rendered (`#chatbot-container` stayed empty).
`_allow_and_capture` now builds a SANITIZED recursive shape
(`_json_shape` / `_json_shape_root`) of the parsed body and discards
the body: per node `type`, object key NAMES + `key_count`, array
`length` (+ first 5 item shapes), string `length`, and for URL strings
only `scheme`/`host`/`path` (query & fragment dropped). Bounds:
depth 6, 50 keys/object, 5 array items, 800-node budget → a
`{"type":"truncated","reason":…}` node + `json_shape_truncated` flag.
The ONLY literal values recorded are booleans and — for
`_SHAPE_SAFE_ENUM_KEYS = {"statuscode","flowtype"}` — a finite number
or a `≤24`-char `[A-Za-z0-9_-]+` string (so `200` / `"NORMAL"` yes; a
UUID / sentence / token / email / path no). Records gain
`request_index` (1-based order; the two identical calls → 1, 2).
Network allowlist is byte-for-byte unchanged; a non-allowlisted request
is still `abort`ed and never `fetch()`ed.

**Response observer rewrite (2026-09-09, after `report(3).json`).** The
live run showed the allowed POST 3×, two `200 application/json`, but
`body_size_bytes: 0` / `json_shape: null`, plus one `TargetClosedError`.
Root cause: capture used `route.fetch()` → read → `route.fulfill()`,
which (a) issued a **duplicate** server-side POST per allowed request,
and (b) tied the captured `APIResponse` to a lifecycle that dies when
the post-Apply flow navigates/closes (the response carries
`applyRedirectUrl`), so `resp.body()` / `route.fetch()` lost the race
→ empty body or `TargetClosedError`; head reads (status, content-type)
won the race, hence "200 but 0 bytes". Fix: the allowlisted request is
now just `_continue`d (browser makes ONE real request) and recorded
REQUEST-side in `allowed_apply_init_requests`; the response is observed
by a `context.on("response")` listener (`_on_response`) that reads only
the `Response` object — never page/context — so a navigation degrades
it to a non-sensitive `note` instead of crashing. `body_size_bytes` is
now `int | None` (None = not safely available). Report exposes
`apply_init_request_count` vs `apply_init_response_count`
(+ `allowed_apply_init_requests` with `resource_type`/`is_navigation`
per request) so genuine SPA retries vs lost/double-observed responses
can be told apart; `_APPLY_INIT_MAX = 50` caps both lists. Allowlist,
default-deny, shape sanitiser, and `_SHAPE_SAFE_ENUM_KEYS` unchanged.

**Post-init transition inspection (2026-09-09, after `report(4).json`).**
The 4th live run: Apply-init `200 application/json` 5994 B, full sanitized
shape captured (10 questionnaire items, 3 skippable, `applyRedirectUrl`
→ `/myapply/saveApply`, `statusCode: 0`), exactly 1 allowed request, no
UI rendered. Next question: what happens right after. `MutatingRequestBlocker`
now, once the first allowed apply-init request is seen
(`_apply_init_marker_seq`), also records — READ-ONLY, sanitized metadata
only — subsequent GET requests (`document`/`xhr`/`fetch`/`other`),
non-apply-init responses (status + media type, **body never read**), and
frame navigations, into `post_init_events`; anything whose path is
`_SAVE_APPLY_PATH = "/myapply/saveApply"` (from `applyRedirectUrl` —
observed, **never allowlisted**) is additionally flagged in
`save_apply_events` / `save_apply_seen`. A mutation to that path is
still default-denied and *also* flagged (`kind: "blocked-mutation"`).
Navigations use a page-level `framenavigated` listener
(`observe_navigation(page)`, called by the orchestrator);
`context.on` stays for route+response. Every event carries a monotonic
`seq` (apply-init request/response now do too) for ordering; lists cap
at `_POST_INIT_EVENT_MAX = 500`. All observers are fully defensive
(frame.url / status raising → `note`, never a raise). No new allowlist,
no selectors, no interaction.

**CAPTCHA false-positive fix (2026-09-09, live-run login failure).**
Every fresh isolated-profile `inspect-apply` login raised a
false-positive `NaukriCaptchaError` at the pre-fill check: the old
`selectors.CAPTCHA_INDICATOR` contained a bare `.g-recaptcha`, and
Naukri's normal login page ALWAYS ships a static
`<div class="g-recaptcha" data-sitekey="..." data-size="invisible">`
container (empty, non-interactive; confirmed in
`inspection_output/apply_20260909_115708/challenge_detected.html` and
`.../apply_20260909_113420/`). Earlier runs "worked" only because the
operator manually logged in during the (needless) human-in-the-loop
pause. Fix (CAPTCHA-scoped only): `CAPTCHA_INDICATOR` no longer
matches a bare `.g-recaptcha` — it now lists genuine challenge
indicators (`iframe[src*='captcha']`, `iframe[src*='recaptcha/api2/bframe']`,
`iframe[title*='recaptcha challenge']`, `#captcha`,
`.g-recaptcha:not([data-size='invisible' i])`). New
`login._captcha_challenge_present(page)` checks that selector AND,
belt-and-suspenders, inspects any `.g-recaptcha` container's
`data-size` (anything other than `"invisible"` → real challenge). The
two `_is_present(page, CAPTCHA_INDICATOR)` call sites in `login()` now
call it. `+ selectors.RECAPTCHA_STATIC_CONTAINER = ".g-recaptcha"`.
`MFA_INDICATOR`, the human-in-the-loop pause, the Apply workflow,
request blocker, allowlist, post-init observer, and resume logic are
untouched.

**Post-submit stale-URL fix (2026-09-09, next live run).** With the
CAPTCHA false positive gone, the login POST fired and the network log
showed `/mnjuser/homepage` fetched — but `login()` raised
`NaukriUnexpectedPageError: Current URL: …/nlogin/login`. Cause:
Naukri's login submit is an **XHR POST** (no document navigation)
followed by a **JS redirect**. `_settle_after_login_submit()` only
called `wait_for_load_state("domcontentloaded"/"load")`, which observe
the *already-loaded* `/nlogin/login` document and return instantly —
they do **not** wait for the subsequent redirect. `login()` then read
a stale `page.url` (`/nlogin/login`) and a not-yet-present auth avatar
→ false `NaukriUnexpectedPageError`. Fix (login-scoped, no sleeps):
`_settle_after_login_submit()` now first does a bounded
`page.wait_for_selector(", ".join(AUTHENTICATED_NAV_INDICATOR,
CAPTCHA_INDICATOR, MFA_INDICATOR), state="attached",
timeout=15000)` — `wait_for_selector` re-evaluates across the
redirect, so it spans the navigation with no fixed delay; a genuine
credential failure just times out and falls through to the unchanged
classification (still raises `NaukriUnexpectedPageError`). The
`domcontentloaded`/`load` settle is kept after it. `login()`'s own
checks (DOM `is_authenticated()` preferred, then URL fragment) are
unchanged. `FakePage` gained `wait_for_selector()`.

**Post-init client-workflow observation (2026-09-09, after `report(4)`).**
Apply-init returns HTTP 200 with the whole workflow as JSON (10 mandatory
`questionnaire` items, `flowType:"default"`, `dataCommitted:false`,
`isLeafNode:false`, `applyRedirectUrl → /myapply/saveApply`) but nothing
renders (`#chatbot-container` empty; no form/modal/iframe/controls; no
`/myapply/saveApply` request). New `PostInitClientObserver`
(`apply_inspection.py`) passively answers *why*, sharing the blocker's
`seq` + post-init gate: `page.on("console"|"pageerror"|"websocket"|
"frameattached"|"framedetached")` + an injected AGGREGATE
`MutationObserver` (`_MUTATION_OBSERVER_SCRIPT`, via `add_init_script` +
one `evaluate`) drained during a **bounded** post-Enter loop
(`_DOM_SAMPLE_COUNT=6` × `_DOM_SAMPLE_INTERVAL_MS=750`) that also re-runs
the EXISTING `extract_application_ui()` classification and stores **counts
only**. Report gains `post_init_client_observations` (`console_events`,
`page_errors`, `websocket_events`, `frame_lifecycle`,
`dom_mutation_summaries`, `dom_samples`, `observer_notes`, `caps`).
Metadata only: console excerpts redacted (URLs→path, emails/tokens/`k=v`
secrets stripped) + 300-char cap, kept for error/warning only; WS records
frame **counts + byte sizes**, never payloads; mutations are
counts/tag-histograms/attach-location selectors + open-shadow-root
counts, never DOM text/attrs/HTML; frame lifecycle is path only. Every
observer is fully defensive (never raises out; failures →
`observer_notes`); all collections cap at `_CLIENT_OBS_MAX=200`. NO new
network permission, NO page interaction (no click/fill/focus/dispatch),
NO Naukri-JS-state inspection. The `MutatingRequestBlocker`
route/response/allowlist code and `extract_application_ui`'s selectors
are byte-identical.

## Stage A — daily match digest (implemented 2026-09-09)

The objective-change implementation. Three persistence axes are kept
**separate** and never conflated:

1. **Discovery** — `Job` / `JobExtraction` / `RunEvent`. `discover_and_store`
   builds a query matrix from the profile (or `discovery_queries`
   override), runs the existing `search_jobs`, then `fetch_job_detail`
   (read-only `page.goto` + text extraction via new `JOB_DETAIL_*`
   selectors — never clicks/fills). Dedup reuses the existing external-id
   / URL / fingerprint / repost primitives.
2. **Recommendation history** — `JobRecommendation`, one row per job per
   daily run (keyed on the CANONICAL job id). Records what was emailed
   and when. A `JobRecommendation` NEVER implies the user applied.
3. **Application history** — `ApplicationHistory` (UNIQUE per canonical
   job) + `ApplicationEvent` (status-transition log). The *only*
   authoritative source of application status. Written **only** by the
   manual `mark-applied` / `mark-status` CLI. Snapshots job title /
   company / url / external id so it survives later re-scrapes.

**Pipeline** (`orchestration/pipeline.py::run_daily_recommendations`,
no scheduling): load profile/resume/registry → upsert candidate →
`DailyRun(STARTED)` → discovery → per job: parse (LLM, optional) +
`score_job` (deterministic — the ONLY suitability score) + `select_resume`
(static registry) → `build_digest` → `render_digest` →
file/console send (`email_status = "dry_run"` when `DRY_RUN`) →
`export_workbook` (never fails the run) → `DailyRun(COMPLETED/FAILED)`.
Every stage writes a short, non-sensitive `RunEvent`.

**Eligibility** (`recommendations/builder.py`, fully deterministic):
- status in `recommendation_exclude_if_status` (default APPLIED /
  INTERVIEW / OFFER / REJECTED / WITHDRAWN) → **excluded regardless of
  cooldown**.
- previously recommended, not applied: `recommendation_cooldown_days == 0`
  → eligible again on the very next run; `> 0` → eligible only once that
  many days have elapsed since the last recommendation.
- never recommended, not applied → eligible.
- reposts/duplicates collapse to the canonical job id everywhere.
- then rank by deterministic score desc (tie-break newest
  `first_seen_at`), cap at `daily_recommendation_limit` (default 10).

**LLM boundary:** JD interpretation (`JobParser`, extract-only) and an
OPTIONAL natural-language narrative in the digest (only when
`explanation_use_llm=true`; the narrative is dropped if it references
application status). The LLM never sets a score, decision, status,
freshness, or URL.

**Excel** (`reporting/excel.py`): `job_search_history.xlsx`, sheets
Jobs / Applications / "Daily Runs", **fully regenerated from the DB on
every call** (hand-edits are not preserved; reposts folded to
canonical). Never writes secrets.

**CLI added:** `run-daily`, `discover --query`, `recommend
--dry-run/--no-dry-run`, `export-excel --path`, `mark-applied <job>
--resume --status --date --note`, `mark-status <job> <status>`,
`applications --status`, `report`. `run-now` → `run-daily`.
`scheduler` is implemented in Stage B (see below). `prepare` / `apply`
raise `NotImplementedError` ("application submission is out of scope").
`doctor` gained daily-digest config + "Digest tables reachable" checks.

**Migration:** new tables only (`ApplicationHistory`,
`ApplicationEvent`, `JobRecommendation`, `RunEvent`). No Alembic — `init_db`'s
`create_all` adds them to an existing DB; no columns added to existing
tables; `DailyRun.applications_submitted` left vestigial at 0.

**New Settings** (`config.py`): `daily_recommendation_limit`,
`recommendation_min_score`, `recommendation_cooldown_days`,
`recommendation_exclude_if_status`, `recommendation_decisions`,
`freshness_new_days`, `discovery_queries`,
`discovery_max_jobs_per_query`, `discovery_max_total_jobs`,
`mark_applied_default_status`, `mark_applied_default_resume_from_selection`,
`email_sender`, `email_output_dir`, `email_subject_prefix`,
`explanation_use_llm`, `excel_export_enabled`, `excel_path`.

## Stage B — real SMTP + scheduler

Two independent additions, neither changing Stage A's pipeline logic:

1. **`SmtpEmailSender`** (`notifications/email.py`) — real delivery via
   stdlib `smtplib` + STARTTLS, selected by setting `EMAIL_SENDER=smtp`.
   `build_email_sender()` requires `SMTP_HOST`/`SMTP_USERNAME`/
   `SMTP_PASSWORD`/`NOTIFY_EMAIL_TO` ALL present, else raises
   `EmailConfigError` (a subclass of the existing `EmailSendError`) —
   a misconfigured scheduled run fails visibly instead of quietly
   writing a digest nobody is watching. `email_sender` still defaults
   to `file`; nothing about existing behavior changes unless this is
   explicitly opted into. Credentials come only from `Settings`, never
   logged; every exception reports only `type(exc).__name__`, matching
   `FileEmailSender`'s existing convention — some SMTP server error
   responses can echo back parts of the failed request.
2. **`naukri-agent scheduler`** (`scheduler/daemon.py`) — an in-process
   alternative to an OS-level cron entry / Task Scheduler task calling
   `run-daily` directly (both remain valid; see README's "Scheduler"
   section for the tradeoff). `build_scheduler()` constructs (without
   starting) an APScheduler `BlockingScheduler` with one daily
   `CronTrigger` at `Settings.daily_run_time` ("HH:MM", validated,
   default `10:00`) in `Settings.timezone`; `run_scheduler()` starts it
   and blocks until interrupted. `_run_job_safely()` wraps each firing
   so an exception is logged (type only) and swallowed rather than
   escaping — APScheduler cancels a job's *future* firings by default
   if one raises, which would silently turn "daily" into "once".
   `daily_run_time` is a new validated `Settings` field ("HH:MM",
   24-hour) — a bad value fails at config-load time, not at 10:00 AM.

Neither addition touches `run_daily_recommendations`, `build_digest`,
`score_job`, or anything upstream of "how the digest gets delivered /
how often the pipeline runs" — Stage A's pipeline logic is unchanged.

## Testing conventions

Three tiers, kept strictly separate — see `tests/manual/README.md`:
- Unit tests — pure logic, no I/O.
- Mocked browser tests (`test_browser_*.py`) — fake `Page`/`BrowserManager`
  objects (`tests/browser_fakes.py`), zero real network/browser dependency.
- Manual/real-integration tests (`tests/manual/`) — require a real
  Naukri account + installed Playwright browsers, marked
  `@pytest.mark.manual`, excluded by default
  (`addopts = "-m 'not manual'"` in `pyproject.toml`). Run explicitly
  with `pytest -m manual`.

Run `pytest` for the full non-manual suite before considering any
change done. As of this handoff: **847 passed, 3 deselected**
(includes further skill-evidence/semantic-matcher/apply-inspection
coverage added after the count below was last written, plus Stage B's
`tests/test_scheduler.py`: 6 tests covering `daily_run_time` validation,
`build_scheduler`'s job/trigger configuration, and `_run_job_safely`'s
exception-swallowing — never calling `BlockingScheduler.start()`, which
blocks forever). Stage A test files (37 tests, at the time they were
added):
`tests/test_recommendation_builder.py` (10 — ranking/cap/cooldown
semantics/repost/LLM-narrative-drop), `tests/test_application_history.py`
(7 — NOT_APPLIED default, idempotent mark-applied + events, canonical
resolution never fuzzy, snapshots), `tests/test_digest_output.py` (6 —
render fields + no secrets, file sender never touches SMTP, Excel
regenerated from DB), `tests/test_fetch_job_detail.py` (3 — extracts +
never clicks/fills, degrades to None, goto failure safe),
`tests/test_daily_pipeline.py` (4 — happy path COMPLETED + RunEvent
trail + digest/Excel written + no email, discovery total-failure →
FAILED, mark-applied between runs excludes, structural no-apply-workflow
guard), `tests/test_new_tables_migration.py` (2 — new tables added to a
legacy DB, rows intact, idempotent), `tests/test_stage_a_cli.py` (5 —
`mark-applied`/`mark-status`/`applications`/`export-excel`/`doctor` via
CliRunner). Shared helpers in `tests/digest_fakes.py`.

Earlier coverage (Stage 1 / 1.5, now frozen) added over the real-run bugs
(report-always-written, `networkidle` login timeout, dead-connection
cleanup mask, `.g-recaptcha` CAPTCHA false positive, post-submit
stale-URL `NaukriUnexpectedPageError`), the apply-init allowlist, the
sanitized response shape, the response-observer rewrite, the post-init
transition inspection, and the post-init client-workflow observation.
`tests/browser_fakes.py` has `simulate_connection_loss()`,
`FakeResponse`/`simulate_response()`, `FakeFrame`/`simulate_navigation()`,
`wait_for_selector()`, `FakeConsoleMessage`/`FakeWebSocket` +
`simulate_console`/`simulate_pageerror`/`simulate_websocket`/
`simulate_frame_attached`/`_detached`, `add_init_script`/`evaluate`.

## Working style expected on this project

- Explain architecture and tradeoffs *before* implementing each
  phase/change, not after.
- Explicit user approval is required before moving to a new phase —
  don't assume "looks done" means "go ahead."
- When something can be verified against the real site vs. guessed,
  say so plainly — don't present an unverified selector or assumption
  as confirmed.
- Prefer the smallest correct fix over broad rewrites, especially
  around `browser/` — the whole point of `selectors.py` isolation is
  that a site-behavior fix should touch as little else as possible.
