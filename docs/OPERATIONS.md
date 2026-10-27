# Operations guide

How to run naukri-agent day to day: the scheduled tasks, phone control, the weekly report, every command, and the switches and
backups worth knowing. For first-time configuration see [SETUP.md](SETUP.md); when something fails see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Scheduled tasks (Windows Task Scheduler)

Seven tasks do the unattended work. They run while you are signed in to Windows (a locked screen is fine).

| Task name | When | Command | What it does |
|---|---|---|---|
| Naukri Agent - Profile Refresh | 09:45 daily | `profile-refresh --execute` | Uploads the next of your resumes so the profile counts as freshly updated |
| Naukri Agent - Daily Jobs | 10:00 daily | `run-daily` | Finds, reads and scores jobs; emails the digest; pings Telegram |
| Naukri Agent - Research | 10:05 daily | `research-jobs --wait-for-digest` | Waits for the digest, then researches the company-website jobs |
| Naukri Agent - Watchdog | 10:30 and 15:00 | `watchdog` | Silent unless a morning step did not happen |
| Naukri Agent - Phone Control | at logon, always on | `telegram-listen` | Listens for `/apply`, `/applied`, `/status`, `/help` |
| Naukri Agent - Phone Guard | every 10 min, 06:00-23:00 | `phone-guard` | Restarts the listener if Windows ended it; sends the daily check |
| Naukri Agent - Weekly Report | Sunday 22:00 | `weekly-report` | Builds and emails the weekly Excel file |

### Registering a task

Each task runs the installed console script, with the project folder as its working directory. The working directory matters:
`.env`, `data/`, `logs/`, `out/` and `config/` are found relative to it, and a wrong one silently falls back to defaults.

```powershell
$project = "C:\path\to\Agentic-Job-Search-Application-Automation-System"   # your project folder
$action = New-ScheduledTaskAction `
  -Execute "$project\.venv\Scripts\naukri-agent.exe" `
  -Argument "run-daily" `
  -WorkingDirectory $project
$trigger  = New-ScheduledTaskTrigger -Daily -At 10:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 6)
Register-ScheduledTask -TaskName "Naukri Agent - Daily Jobs" -Action $action -Trigger $trigger -Settings $settings
```

Use the same pattern for the others, changing the command, trigger and time limit. `-StartWhenAvailable` makes a task that was
missed (the PC was off) run when it can, and `-WakeToRun` wakes a sleeping PC. The phone guard has its own script:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_phone_guard.ps1
```

It registers the guard (hidden, through `pythonw.exe`) and switches off "run only if a network is available" on the Phone
Control task, so a Wi-Fi blip does not end it. Windows' time zone should be your own; Task Scheduler has no per-task time zone.

### Things the schedule depends on

- **Ollama must be running** at 10:00 if it is your model provider. It is a per-user app that starts at login, not a Windows
  service, so do not tick "Run whether user is logged on or not", or parsing will fail with a connection error to
  `localhost:11434`.
- **A run needs a human only for CAPTCHA, one-time codes or an expired session.** The login stops immediately with a clear
  error instead of waiting, the run is marked `FAILED` with the exception type, and the watchdog tells you on Telegram.
- **Test a task without waiting:** right-click it in Task Scheduler and choose Run. That executes the real command, so do it
  only when you are ready for a real run.
- **An alternative** to Task Scheduler is `naukri-agent scheduler`, a single foreground process that fires the daily run once a
  day at `DAILY_RUN_TIME`. Use one or the other against the same database, never both.

## Applying from your phone

`telegram-apply` needs a terminal on the PC. To start it from your phone instead, leave `naukri-agent telegram-listen` running
(the Phone Control task does that) and set `TELEGRAM_REMOTE_START=true` in `.env`. From your own chat with the bot:

| Send | What happens |
|---|---|
| `/apply` | starts `telegram-apply`; a card per job arrives, and nothing is applied until you tap Yes |
| `/applied` | asks Applied / Not applying / Later for company-website jobs that have no answer yet |
| `/status` | how many jobs are ready and how many approvals are left today |
| `/help` | the list of commands |

How it stays safe: only your configured chat is obeyed; an `/apply` older than `TELEGRAM_LISTENER_MAX_COMMAND_AGE_SECONDS`
(default 600) is ignored; `AUTO_APPLY=true` and `DRY_RUN=false` are set only for the one `telegram-apply` process it starts;
the listener never imports apply code; and while an apply run is going it does not read Telegram, because the run is reading
your taps. The PC must be on and signed in, since the clicking happens in its browser.

**One card, one tap.** Each job's Yes/No buttons belong to that card alone. A second tap on an earlier card answers nothing, so
it can never approve the next job. Your tap is acknowledged within seconds: the card turns into one grey "Applying... please
wait" (or "No - skipped") button.

**The questions.** The app asks you only the question Naukri's form is asking right now ("Question 1", "Question 2", ...),
because the form asks only what it cannot fill from your profile or earlier tries, even when Naukri's data lists more. A typed
answer gets a short Yes/No before it is sent; a choice is sent when you tap. If the form closes while you are thinking, the
app reopens it and sends your saved answer. Anything unexpected sends nothing and hands the job back to you. Each attempt saves
`inspection_output/auto_apply/<attempt>_form.json` (what Naukri listed versus what its form asked).
`APPLY_FOLLOW_LIVE_FORM=false` restores the older "ask everything, then type everything" behaviour.

Limits: at most 4 applications per rolling 24 hours, only jobs that score 80 or more with a Naukri Apply button, never contract
or temporary jobs. Pause with `data/PAUSE_AUTO_APPLY`.

### Keeping phone control alive while you are away

Windows ends the listener's task whenever the PC sleeps or the network blips. Three things keep it running:

- The listener holds the PC awake from 06:00 to 23:00 (`TELEGRAM_LISTENER_AWAKE_FROM` and `_UNTIL`).
- The **phone guard** restarts the listener within 10 minutes if it is not running, and every scheduled command and the watchdog
  do the same check first.
- Once a day (`ALIVE_MESSAGE_TIME`, default 10:50) the guard sends one Telegram message: the PC is on, phone control is
  running, what the job search did, and how many jobs wait for `/apply`. **If it does not arrive, the PC or its internet is
  down.** A frozen or shut-down PC cannot alert you, so silence is the signal.

A PC that is shut down cannot run any task. If you will be away for long, leave it on and set Windows to sign in automatically
after a restart, because a restart that stops at the sign-in screen stops everything. Try the guard without effect with
`naukri-agent phone-guard --dry-run`.

## "Did you apply?" for company-website jobs

The app cannot see what you do on a company's own site, so it asks on Telegram, one job at a time, with three buttons:

| Button | What is recorded | Label in the history |
|---|---|---|
| **Applied** | applied, with the resume that was suggested | `applied through company website` |
| **Not applying** | a decision; never suggested again | `not applied` |
| **Later** | nothing yet; asked again. The 4th Later in a row records it as ignored | `ignored` |

Jobs the app applied to after your Yes are labelled `applied directly`. It asks when you send `/applied`, and once a day at
`FOLLOWUP_REMINDER_TIME` (20:00) when a job is waiting; it stays silent when nothing is. It covers jobs from the last
`FOLLOWUP_LOOKBACK_DAYS` (7) days, at most `FOLLOWUP_MAX_JOBS` (12) at a time. No tap within `FOLLOWUP_ANSWER_TIMEOUT_MINUTES`
(15) stops the questions and changes nothing. Part 3 of the digest lists where each of these jobs stands.

## The weekly report

Every Sunday at 22:00, `naukri-agent weekly-report` builds one Excel file with every job given that Monday-to-Sunday week and
where each stands, saves it under `WEEKLY_REPORT_DIR` (default `out/weekly/`), and emails it to `NOTIFY_EMAIL_TO`.

- **Sheets:** `Summary` (totals by route and status) and `Jobs` (one row per job: date, route, title, company, location, score,
  resume, status and its date, direct apply link, Naukri link, notes), coloured by status.
- **A job marked failed or unconfirmed is flagged.** Check it on Naukri: the app could not confirm the result, and it may
  still have gone through.
- By hand: `naukri-agent weekly-report` (this week on a Sunday, otherwise the week that just ended), `--week-of 2026-10-07` for
  a chosen week, `--no-email` to only save the file.

## The digest and the records

The daily run's output is a digest: at most 10 jobs a day, only those scoring 80 or more, so fewer is normal.

- **Part 1:** company-website jobs, each with the employer's direct apply link read from the job data Naukri's page already
  loads. You apply yourself.
- **Part 2:** jobs the app applied to for you since the last digest.
- **Part 3:** where recent company-website jobs stand.

The database (`data/naukri_agent.db`) is the source of truth. Excel files are copies rebuilt from it. A job you applied to, or any
status in `RECOMMENDATION_EXCLUDE_IF_STATUS`, is never recommended again; a job recommended but not applied follows
`RECOMMENDATION_COOLDOWN_DAYS`.

## All commands

| Command | What it does |
|---|---|
| `doctor` | Check the environment, profile, resumes and database |
| `run-daily` (alias `run-now`) | Discover, read, score, digest, Excel, Telegram ping |
| `recommend` | The full daily pipeline (use `--dry-run` to write the digest to a file) |
| `discover` | Read-only discovery only |
| `match` | Run job matching only |
| `report` | Re-print the latest digest |
| `export-excel` | Regenerate the Excel workbook from the database |
| `applications` | List recorded applications (the authoritative history) |
| `mark-applied`, `mark-status` | Record an application you made yourself, or set a job's status |
| `profile-refresh` | Upload the next resume (`--execute` to actually do it) |
| `research-jobs` | Research the company-website jobs (`--wait-for-digest`, `--max-jobs`, `--job-id`, `--dry-run`) |
| `telegram-setup` | One-time: finds your chat id |
| `telegram-apply` | Offer ready Naukri Apply jobs to you on Telegram (`--max-jobs`) |
| `telegram-listen` | Stay running and wait for `/apply` and friends |
| `phone-guard` | Restart the listener if needed; send the daily check (`--dry-run`) |
| `watchdog` | Check that the morning chain happened (`--dry-run`) |
| `weekly-report` | Build and email the weekly Excel report |
| `scheduler` | The in-process daily scheduler (an alternative to Task Scheduler) |
| `inspect`, `inspect-apply`, `inspect-profile-edit` | One-off read-only inspection tools for developers |
| `auto-apply` | Unattended apply, off unless `AUTO_APPLY_UNATTENDED=true`; not used |
| `apply`, `prepare` | Older commands kept but not part of the daily flow |

## Switches, backups and housekeeping

- **Pause switches:** create the file `data/PAUSE_AUTO_APPLY` or `data/PAUSE_PROFILE_REFRESH` to stop applying or the refresh.
- **Back up the database** before editing it by hand: copy `data/naukri_agent.db` to a dated name. New tables and nullable
  columns are added automatically at startup, so there is no migration tool.
- **Personal data stays local.** `.env`, `config/*.yaml`, `resumes/`, `data/`, `logs/`, `out/` and `inspection_output/` are
  git-ignored.
- **Two commands never share the Naukri browser at once.** They take turns through a lock file in `data/`.
