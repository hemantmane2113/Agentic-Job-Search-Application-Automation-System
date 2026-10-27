# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project
has no tagged releases yet, so entries are grouped by date, newest first.

## 2026-10-22 to 2026-10-27: reliable phone control and a smarter apply form

### Added
- **Phone guard**: a task that runs every 10 minutes (06:00-23:00), restarts the phone listener if Windows ended it, and sends
  one "Daily check: the PC is on" Telegram message a day. No message means the PC or its internet is down.
- The phone listener holds the PC awake from 06:00 to 23:00 and every scheduled command restarts it first if it is not running.
- A 19:00 reminder listing the Naukri Apply jobs still waiting.
- Live-form applying: the app asks only the question Naukri's form is asking right now, one at a time, and sends each answer at
  Naukri's pace. It saves `_form.json` evidence showing what Naukri listed versus what its form asked.
- Interactive diagrams and a diagrams section on the README.
- The MIT License and a Code of Conduct (Contributor Covenant 2.1).
- Setup, operations and troubleshooting guides, this changelog, contributing and security notes, issue and pull-request
  templates, and a test workflow.

### Changed
- Every Telegram button belongs to one prompt, so a second tap on an earlier card can never approve the next job. A tap is
  acknowledged within seconds ("Applying... please wait" or "No - skipped").
- A resume change after an application opens a fresh browser first, because the safety guard blocks uploads once a form has been
  opened.
- A database write waits up to 30 seconds for another writer instead of failing at once.
- The README was rewritten to describe the system as it is now.
- Real employer names in comments, tests and notes were replaced with made-up ones.

### Fixed
- Answers typed into questions Naukri never asked, which lost an answer or ended in Naukri's own error page.
- A phone listener that stayed dead after the PC slept, so `/apply` went unheard for days.

## 2026-10-08 to 2026-10-21: the daily plan, research and phone apply

### Added
- Applying through Telegram: each job arrives as a card, and nothing is applied without a Yes tap; choice and typed questions
  are answered on the phone.
- The company-site researcher, a read-only AI agent that finds the careers page and how applying works there, with a research
  email and the employer's direct apply link read from Naukri's own job data.
- Daily plan of at most 10 jobs scoring 80 or more, recency in the score, and exclusion of contract and temporary jobs.
- A morning watchdog, the daily profile refresh by resume rotation, a Telegram ping when jobs are ready, and `/apply`, `/applied`
  and `/status` from the phone.
- "Did you apply?" questions for company-website jobs, application labels, Part 3 of the digest, and the Sunday weekly Excel
  report.
- Keep-awake while a long job runs, and one Naukri browser at a time through a lock file.

### Changed
- Discovery reads jobs not seen before first and uses as few searches as it can (at most 24 a run).
- A failed answer no longer hides a finished application: Naukri's own "Applied" marker is checked before anything is called
  failed, and a failure saves a screenshot and the form's HTML.

### Removed
- The LinkedIn and cold-email code, because both plans were dropped.

## 2026-09-08 to 2026-09-17: the foundation

### Added
- Project scaffolding: validated settings, SQLAlchemy and SQLite, logging, and the `naukri-agent` command line.
- Your candidate profile, master resume and resume registry; the deterministic job scorer; the AI job parser with a provider
  abstraction (Ollama, Groq, OpenAI).
- Read-only Naukri discovery with Playwright, the daily match digest, the application history and the Excel mirror.
- Real SMTP email delivery (opt-in) and an in-process scheduler.
