# Security policy

## Reporting a problem

If you find a security problem, for example a way to apply without the user's approval, a secret that could leak into a log or
a message, or a way to make the app fetch something it should not, please **do not open a public issue**.

Report it privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Include what
you found, how to reproduce it, and the version (commit) you tested. You will get a reply as soon as the owner can; this is a
personal project maintained by one person, so please allow some days.

## What is in scope

- Any path by which an application could be submitted without the user's explicit Yes on that job.
- Any way secrets (Naukri login, API keys, the Telegram token, SMTP password) could appear in logs, messages, the database or
  the repository.
- Prompt injection through job postings or web pages the research agent reads, and server-side request forgery in that agent's
  fetches (it is meant to be https-only, public addresses only, and size-capped).
- The phone listener obeying anyone other than the configured chat.

## What the project already does

- Keeps credentials only in `.env`, which is git-ignored, and reports errors by exception type only.
- Obeys only the one configured Telegram chat, and ties each Yes/No button to a single prompt.
- Treats job postings and web pages as untrusted text, validates the model's reply against a whitelist schema, and keeps the
  research agent read-only.
- Blocks every non-read network request during an application except two exact endpoints.

## If you use the project yourself

Never commit `.env`, `config/*.yaml`, `resumes/`, `data/`, `logs/`, `out/` or `inspection_output/`; they hold personal data.
If a token or password ever reaches a commit, treat it as leaked: revoke and replace it first, then remove it from history.
