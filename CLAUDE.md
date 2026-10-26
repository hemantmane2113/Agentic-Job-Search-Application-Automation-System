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
  config.py  power.py            settings; keep Windows awake during long jobs and while the phone listener runs
  safe_error.py                  one-line, secret-scrubbed error text for the log file only
scripts/                         register_phone_guard.ps1 (the 10-minute guard task), repair_stale_reposts.py
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
`weekly-report` · at logon `telegram-listen` (phone control, always on) · every 10 minutes 06:00-23:00 `phone-guard` (hidden, via
pythonw). The 19:00 reminder and the 20:00 "did you apply?" question are sent by the listener; the once-a-day "PC is alive"
message (10:50) by `phone-guard`.

## Things to know before changing anything

- **The apply panel is the fragile part.** Naukri's chat widget varies: question types (radio `label.ssrc__label`, checkbox
  `label.mcc__label`, free text in the drawer), order, and skipped questions. After any apply failure read
  `inspection_output/auto_apply/<attempt>_failure.{png,html}` before guessing. Any new type should produce a clean stop with a snapshot.
- **Do not change `browser/apply_inspection.py`** except to fix a real defect: its `MutatingRequestBlocker` is the safety net for
  applying. Its inspection tooling (`inspect-apply`) is frozen.
- **Naukri's login page can flash its form before redirecting a logged-in session.** `login()` waits for a late redirect; keep that.
- **Telegram's connection drops now and then.** Waiting for a tap must survive a dropped connection (`TelegramChannel._wait`).
- **Every Telegram button belongs to ONE prompt** (`callback_data` carries a prompt id: `y:<id>`, `opt:<id>:<n>`). A tap on any
  other card answers nothing. Never go back to bare `y` / `n` / `opt:N`: a second tap on the previous job's card (people tap
  again when the app takes seconds to react) was read as approval of the NEXT job (2026-10-21: Eduitalent "approved" 27 s
  after the previous job). An answered card's buttons turn into one grey button: "Applying... please wait" or "No - skipped".
- **A tap is acknowledged within seconds, while the browser is busy** (`telegram_instant_ack`, default on): a small background
  reader (`TelegramChannel._watch_loop`) watches the ONE card just sent. Telegram allows one `getUpdates` reader at a time, so
  anything else that reads Telegram while a card is out must call `drain()` / `_stop_watch()` first (all the `ask_*` calls do),
  and a job that is skipped without an answer must call `cancel_approval()` (the runner's `record()` does). The reader never
  decides: `wait_yes_no()` still returns the answer. Without it the app read taps only 10-30 s later and people tapped twice.
- **A sleeping PC cannot hear Telegram.** A PC woken by a scheduled task sleeps again as soon as the task ends (found 2026-10-21:
  the PC slept 1-5 minutes after each morning message and `/apply` went unheard for six days). The listener holds the PC awake
  06:00-23:00 (`power.AwakeWindow`); keep that. A PC that is SHUT DOWN cannot be woken by any task.
- **Windows ends the listener's task whenever the PC sleeps** (0x8007042B, "process terminated unexpectedly") and the task is
  only started at logon, so after one sleep nothing heard `/apply` and nothing kept the PC awake (15-20 Oct; again 22 Oct 09:30,
  when a 09:00 boot slept before the old 09:40 window opened). So every scheduled command (profile-refresh, run-daily,
  research-jobs, weekly-report) and the watchdog first calls `orchestration/phone_control.ensure_phone_control_running`, which
  starts the task again if it is not Running; the watchdog also tells you. Tests never reach the real Task Scheduler
  (`tests/conftest.py`).
- **The owner is often away from the PC for weeks, so SILENCE must be a signal.** A frozen or switched-off PC cannot alert anyone.
  `naukri-agent phone-guard` (task "Naukri Agent - Phone Guard", `scripts/register_phone_guard.ps1`) restarts a dead listener
  within 10 minutes and sends ONE "Daily check ... the PC is on" Telegram message a day (`alive_message_time`, 10:50). No message
  = something is wrong with the PC or its internet. The Phone Control task has "run only if a network is available" switched
  OFF (a Wi-Fi blip used to end it). The guard only sends, never reads, Telegram.
- **Naukri's question form must not sit open while the person answers on Telegram, and answers must go in at Naukri's pace.**
  Found 22-26 Oct (Tiger Analytics failed three ways): after 2 min 28 s open, four answers typed in ONE second gave Naukri's own
  "error while processing your job application"; after 4 min 40 s the form had closed. So: (1) `submit_answer` waits after each
  Save until the bot adds its next `li.botItem` (VERIFIED selector) or the form closes, and returns `next|closed|timeout|skipped`;
  a `closed` form means Naukri finished early, so the runner stops answering and checks "Applied" (no more spurious errors);
  (2) if collecting the answers took longer than `apply_panel_max_wait_seconds` (60; 0 = off) the runner reopens the form
  (`NaukriClient.reopen_application`: reload the job page, click Apply, read the questions) and replays the saved answers matched
  by question text (`_replay_plan`); anything it cannot match sends NOTHING and hands the job back. **UNVERIFIED against live Naukri:**
  the reopen path (a job's Apply clicked a second time) has only run against fakes; the first real use will show. Do not test it by
  pressing Apply on the account.
- **Answer what the form ASKS, not what Naukri's data LISTS** (`apply_follow_live_form`, default on; `_follow_live_form` in
  `auto_apply_runner.py`). Found 23-26 Oct: Naukri's data listed 7 questions for Tiger Analytics but its form asked ONE
  ("... experience in Deployment?"): it keeps the answers it already has (after a failed try it asks only what is left) and fills
  notice period and pay from the profile. Typing the whole list put answers into questions never asked (the saved panel of 10 Oct
  shows the bot replying "Sorry! I am not trained to answer this query :("), in a one-second burst that lost one. So the runner
  reads the form's current question (`ApplyWorkflowSession.read_live_question`: last non-"sorry" `li.botItem`, the choices on
  screen, whether the text box is showing; it must be the same on two looks), asks the person ONLY that on Telegram ("Question 1",
  no total), confirms typed answers with a short Yes/No (`confirm_answer`), and sends only if the form is STILL asking that exact
  question (`answer_live_question` raises `FormChanged` and types nothing otherwise). It then waits for the bot's next message and
  repeats until the form closes (15-question cap; a question asked a third time, a form asking nothing answerable, or two reopenings
  = hand the job back; a form-caused handback first checks whether Naukri shows the job as Applied). If the form closes while the
  person thinks, it is reopened and the saved answer reused. `<attempt>_form.json` records what was listed vs asked.
  **UNVERIFIED against live Naukri:** this whole flow has only run against scripted copies of the real page (the `li.botItem`,
  choice-label and text-box selectors are verified from the saved 10 Oct panel); the first real use will tell. Do not test it by
  pressing Apply on the account.
- **The network guard stays armed for the rest of a browser session** after the first application form is opened, and it also blocks
  the resume UPLOAD (21 Oct: Eduitalent's resume change failed; the log shows `BLOCKED ... filevalidation.naukri.com/file`). So before
  a resume change in a session that has already opened a form, the runner starts a fresh browser (`session_armed`). A resume change
  that cannot be confirmed saves `<attempt>_resume.png/.html` and says what Naukri's profile showed.
- **One Chrome profile, one command at a time.** `BrowserManager.launch` takes `browser/profile_lock.py`'s file lock and waits its
  turn; do not open the persistent profile any other way.
- **Errors report only the exception type** everywhere except the local log file, where `safe_error_text` also writes the class
  name and a scrubbed first line (secrets replaced by `***`); never put that text in the database, an email or Telegram.
- **Statuses:** `ApplicationStatus` has NOT_APPLYING and IGNORED besides the original ones; both are excluded from digests by default.
- **Windows specifics:** use PowerShell for Task Scheduler; the shell tool is Git Bash. Avoid `rm -rf` with shell variables.

## Testing

Three tiers: unit; mocked-browser (`test_browser_*.py`, no real browser); manual (`pytest -m manual`, excluded by default). Run
`pytest` before calling a change done. Currently **1771 passed, 3 deselected**. A fix for a live bug gets a test built from the
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
