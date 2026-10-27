# Setup guide

How to install naukri-agent and configure it for your own Naukri account. Allow about 30 minutes, most of it for the Telegram
bot and the email app password. Every step is optional except the first four; the later ones switch features on.

| Step | Needed for |
|---|---|
| 1. Install | everything |
| 2. Environment file | everything |
| 3. Your profile and resumes | scoring and choosing a resume |
| 4. Check it with `doctor` | everything |
| 5. An AI model | reading job postings |
| 6. Telegram | applying from your phone, alerts, the evening questions |
| 7. Email | the daily digest and the weekly report |
| 8. Company-site research | the research email |

## 1. Install

You need **Python 3.12 or newer** and Git. Windows is the primary platform, because the scheduling and keep-awake features use
Windows Task Scheduler. The scoring, digest and database parts also run on macOS and Linux.

```bash
git clone https://github.com/hemantmane2113/Agentic-Job-Search-Application-Automation-System.git
cd Agentic-Job-Search-Application-Automation-System
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium       # one-time download of the browser the app drives
```

## 2. The environment file

```bash
cp .env.example .env
```

Open `.env` and fill in your values. The file is git-ignored; never commit it and never paste its contents into a chat or an
issue. `.env.example` explains every setting. The ones you will touch first:

| Setting | What it is |
|---|---|
| `NAUKRI_EMAIL`, `NAUKRI_PASSWORD` | your Naukri login, read only by the browser code |
| `TIMEZONE` | default `Asia/Kolkata` |
| `DRY_RUN` | `true` (default): the daily run writes the digest to a file and sends no email |
| `AUTO_APPLY` | `false` (default): nothing can be applied. `telegram-apply` sets this to `true` for its own process only |
| `LLM_PROVIDER`, `LLM_MODEL` | which AI model reads job postings, for example `ollama` and `llama3.1` |
| `EMAIL_SENDER` | `file` (default), `console` or `smtp` |

Leave `DRY_RUN=true` and `AUTO_APPLY=false` in `.env`. The apply command flips them for the single process it starts.

## 3. Your profile and resumes

The three files below hold personal data, so they are git-ignored. Start from the examples:

```bash
cp config/candidate_profile.example.yaml config/candidate_profile.yaml
cp config/master_resume.example.yaml     config/master_resume.yaml
cp config/resumes.example.yaml           config/resumes.yaml
```

- **`candidate_profile.yaml`**: your search criteria (roles, cities, skills, years of experience, salary, notice period). It
  drives the searches and the scoring and answers recruiters' routine questions. The file documents each field.
- **`master_resume.yaml`**: your career history, used for matching and as a factual record. Nothing is written from it.
- **`resumes.yaml`**: the list of your own resume files and the roles each covers. Put the PDFs in `resumes/` (also
  git-ignored). The app never writes or edits a resume; it only chooses between yours.

## 4. Check the setup

```bash
naukri-agent doctor
```

`doctor` checks the Python version, that your settings load, the AI model configuration, the database, that your profile and
resume files exist and parse, and that every registered resume file is on disk. It names the exact check that failed.

Then try one safe run:

```bash
naukri-agent run-daily
```

With the defaults it logs in, searches, reads and scores jobs, and writes the digest under `out/emails/`. It sends nothing and
applies to nothing. If Naukri shows a CAPTCHA or asks for a one-time code, the run stops at once with a clear error and never
waits on it or tries to get past it. A person has to clear it: `NAUKRI_HEADLESS=false` (the default) keeps the browser
visible so you can. The session is kept in `data/browser_profile`, so once you are signed in the next run reuses it.

## 5. An AI model

The app uses a model only to extract fields from job postings, and in the company researcher. Pick one provider:

| Provider | Set in `.env` | Notes |
|---|---|---|
| **Ollama** (local, default) | `LLM_PROVIDER=ollama`, `LLM_MODEL=llama3.1`, `OLLAMA_HOST=http://localhost:11434` | Free and private. Install [Ollama](https://ollama.com), run `ollama pull llama3.1`, and keep it running. Parsing 50 jobs takes a while on a laptop. |
| **Groq** | `LLM_PROVIDER=groq`, `GROQ_API_KEY=...` | Hosted and fast. Needed anyway for the company researcher. |
| **OpenAI** | `LLM_PROVIDER=openai`, `OPENAI_API_KEY=...` | Hosted. |

The provider (who serves the model) and the model (which one to ask for) are set independently.

## 6. Telegram (phone control)

1. In Telegram, message **@BotFather**, send `/newbot` and follow the steps. Copy the token it gives you.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN=...`. Keep the token only there.
3. Open your new bot in Telegram and send it any message.
4. Run `naukri-agent telegram-setup`. It prints your chat id. Put it in `.env` as `TELEGRAM_CHAT_ID=...`. **Only that one chat is
   ever obeyed.**
5. To start applying from your phone, also set `TELEGRAM_REMOTE_START=true`, then keep `naukri-agent telegram-listen` running
   (see [OPERATIONS.md](OPERATIONS.md)).

## 7. Email

Set `EMAIL_SENDER` to one of:

- **`file`** (default): the digest is written to `out/emails/digest_<timestamp>.txt`. Nothing is sent.
- **`console`**: printed to the terminal.
- **`smtp`**: a real email. All four of `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD` and `NOTIFY_EMAIL_TO` must be set, or the
  run fails with a clear error instead of quietly writing a file.

For Gmail: `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, your address as `SMTP_USERNAME`, and a Gmail **app password** (not your
normal password) as `SMTP_PASSWORD`. Create one at <https://myaccount.google.com/apppasswords>; Google requires 2-Step
Verification first. Any standard STARTTLS provider works the same way. Credentials are read only from `.env` and never
logged; failures name only the exception type.

## 8. Company-site research (optional)

For jobs that say "Apply on company site", `naukri-agent research-jobs` runs a read-only AI agent that finds the company's
careers page and how applying works there. It needs:

- `GROQ_API_KEY` (get one at <https://console.groq.com>), and
- a web-search key: `SERPER_API_KEY` (<https://serper.dev>) or `BRAVE_API_KEY`.

Without a search key it still runs, with weaker results. The settings that control cost and scope (`RESEARCH_MAX_JOBS`,
`RESEARCH_MAX_STEPS`, the daily token budget) are in `.env.example`. The researcher cannot apply, log in or fill a form.

## Next

Schedule the daily run and the phone listener: [OPERATIONS.md](OPERATIONS.md). If something does not work:
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).
