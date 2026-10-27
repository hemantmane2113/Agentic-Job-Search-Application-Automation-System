# Contributing

Thanks for your interest. naukri-agent is a personal tool that automates one person's own Naukri job search, so changes are
reviewed against a short list of rules that keep it safe. Please read them before you open a pull request.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md). By contributing you agree that your contribution is released under the
project's [MIT License](LICENSE).

## Rules the code must keep

These are enforced by tests or by review. A change that breaks one will not be merged.

1. **A human approves every application.** Nothing is applied without the user's Yes tap on that job. The daily run, discovery,
   the scheduler, the research agent, the phone listener and the follow-up must never import apply code (structural tests check
   this).
2. **Never bypass CAPTCHA, MFA, rate limits or anti-bot protection.** Detect them and pause for a person. No stealth, no evasion.
3. **Never press "Apply on company site".** Naukri marks the job applied when it is pressed. Read the employer's address from
   the job data the page already loads.
4. **The AI never decides.** Scoring, ranking, eligibility and statuses are plain code. The parser only extracts; the research
   agent only reads.
5. **The database is the truth.** `ApplicationHistory` is the only record of what the user did. Excel files are copies.
6. **Selectors live in `browser/selectors.py` only**, each marked VERIFIED (with date and source) or UNVERIFIED. Do not present
   a guess as confirmed.
7. **No resume generation.** `resume/` only chooses among the user's own files.
8. **Skill matching is a small alias table, never capability inference** ("Python" does not imply "Django").
9. **Secrets stay out of everything.** Never commit `.env`, personal config, resumes, databases, logs or captured pages. Errors
   report only the exception type.
10. **`DRY_RUN=true` and `AUTO_APPLY=false` stay the defaults.**

## Set up for development

```bash
git clone https://github.com/hemantmane2113/Agentic-Job-Search-Application-Automation-System.git
cd Agentic-Job-Search-Application-Automation-System
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # a dummy .env is enough to run the tests
pytest
```

The default test run uses fake browser pages and needs no network, credentials or Naukri account. Tests marked `manual` (under
`tests/manual/`) need a real account and are excluded unless you run `pytest -m manual`.

## Making a change

1. **Open an issue first** for anything that changes behaviour, so the design can be agreed before code is written.
2. Branch from `main`: `git switch -c fix/short-description`.
3. Keep the change small and focused. Match the surrounding code: naming, comment density, idiom.
4. **Write a test.** A fix for a bug seen on the real site gets a test built from the real shape that failed (see
   `tests/test_choice_questions.py` for an example). Run the whole suite; it must pass.
5. **Say what is verified and what is a guess.** If a change depends on how Naukri's real page behaves and you have not seen it
   run against the real site, mark it UNVERIFIED in the code and in the pull request.
6. Update the docs that describe the behaviour you changed (`README.md`, `docs/`, and the diagrams if the flow changed).

## Commit messages and pull requests

- Start with a short summary line that says what changed, then a paragraph on **why**. Describe the problem the change solves.
- Reference the issue number.
- In the pull request, list what you ran (`pytest`: how many passed) and what you could not test.
- Do not include personal data, screenshots of a real profile, tokens or real job-application details.

## Reporting a bug

Use the bug-report issue template. Attach the relevant lines of `logs/naukri_agent.log` and, for a failed application, the
`_failure.png` and `_form.json` files, with anything personal redacted. Report a security problem privately, as described in
[SECURITY.md](SECURITY.md).

## Code layout

`README.md` has the short map and [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) explains the design and where to look when
something breaks. `CLAUDE.md` holds the working notes used with an AI coding assistant; it is useful background but not required
reading.
