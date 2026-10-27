# Troubleshooting

Start with `naukri-agent doctor`. It checks the Python version, that your settings load, the AI model configuration, the
database, that your profile and resume files exist and parse, and that every registered resume is on disk, and it names the
check that failed.

## Where to look

| Evidence | Where |
|---|---|
| The log of every run | `logs/naukri_agent.log` |
| What each stage of a daily run did | the `run_events` table in `data/naukri_agent.db` (one row per stage) |
| A failed or unconfirmed application | `inspection_output/auto_apply/<attempt>_failure.png` and `.html`: a screenshot and the form's HTML |
| What Naukri listed versus what its form asked | `inspection_output/auto_apply/<attempt>_form.json` |
| A login problem | `inspection_output/login_diagnostics/` |
| A resume change that could not be confirmed | `inspection_output/auto_apply/<attempt>_resume.png` and `.html` |
| An alert about the morning chain | your Telegram chat, from the watchdog |

Read the saved failure files before guessing. Errors in messages and the database name only the exception type; the log file
also holds a scrubbed first line. Secrets are replaced by `***`.

## Problems seen so far

**`/apply` is not answered.**
Check the daily check message first: if it did not arrive today, the PC or its internet is down. A sleeping PC cannot hear
Telegram, and Windows ends the listener's task when the PC sleeps. The listener normally holds the PC awake from 06:00 to 23:00
and the phone guard restarts it within 10 minutes, so wait 10 minutes and try again. In `logs/naukri_agent.log`, "telegram
listener: listening" lines every half hour mean it is alive; a "nothing was heard for N hours" line means it was not running.
A PC that is shut down can only be switched on by a person. An `/apply` older than 10 minutes is ignored on purpose.

**A scheduled run did not happen.**
The watchdog (10:30 and 15:00) tells you on Telegram. Check that you were signed in to Windows, that the task's working
directory is the project folder, and the task's history in Task Scheduler. A missed task runs when the PC is next available
only if "start when available" is set.

**The run stopped at login with a CAPTCHA or one-time-code error.**
Expected: the app never solves or waits on these. Sign in to Naukri yourself in the visible browser (`NAUKRI_HEADLESS=false`);
the session is kept in `data/browser_profile`, so the next run reuses it. Naukri's login page sometimes flashes its form before
redirecting a signed-in session; the app waits for that late redirect, so a single flash in the log is not an error.

**Parsing fails with a connection error to `localhost:11434`.**
Ollama is not running. It starts at login as a per-user app; do not run the scheduled task when you are signed out.

**"N job(s) could not be parsed by the LLM".**
Almost always the model provider. Check `OLLAMA_HOST`, that the model is pulled, or the Groq/OpenAI key. A failed parse is
retried once and never produces a recommendation from the raw listing alone.

**An application ended "failed" or "unconfirmed".**
The app counts a job as applied only when Naukri's own page shows "Applied", and checks that marker again after any error
following the Apply click. If neither is found it records "unconfirmed" and stops. Open the job on Naukri to see whether it
went through, then record it with `naukri-agent mark-applied <job>`. Read the failure screenshot and `_form.json` to see which
question the form asked.

**The form asked something the app did not expect.**
It sends nothing and hands the job back to you, with a screenshot saved. Anything already sent is kept by Naukri, so applying
by hand asks only what is left. A new question type should be reported as an issue (see below) with the saved files.

**"The database is busy".**
Only one process writes to the SQLite database at a time. A write waits up to `DATABASE_BUSY_WAIT_SECONDS` (default 30) for the
other to finish, so seeing this usually means the daily run is still going. Try again a few minutes later.

**Two commands collided on the browser.**
They should not: the Naukri browser profile is shared through a lock file (`data/browser_profile.lock`), and the second command
waits its turn. If a command seems stuck for a long time, a previous one may still be running; check for a leftover Chrome
process.

**A selector stopped working after a Naukri change.**
Every selector lives in `src/naukri_agent/browser/selectors.py`, marked VERIFIED (with a date) or UNVERIFIED. Update it from
what you see in the saved page HTML; nothing else should need to change.

## Still stuck

Open an issue using the bug-report template and attach the relevant lines from `logs/naukri_agent.log`. Do not attach `.env`,
`data/`, screenshots or HTML that show your profile, or anything with a token or password. Redact before you paste.
