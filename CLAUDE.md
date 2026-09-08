# naukri-agent — project context for Claude Code

This file is read automatically by Claude Code at the start of every
session. It exists so work can continue here with full context,
picking up exactly where a prior chat-based session (claude.ai) left
off — read this before making any changes.

## What this is

An agentic job-search assistant for Naukri.com: discovers jobs, scores
them deterministically against a candidate profile, selects an
existing (never generated) resume file per application, and — once
Stage 2 is built — will refresh/apply on Naukri with a human approving
every write action. Built from a detailed master-prompt spec,
developed in strict incremental phases with explicit approval required
between phases.

## Ground rules that must never be violated

- **Never bypass CAPTCHA, MFA, rate limits, or anti-bot protections.**
  Detect and pause for human intervention; never attempt to solve.
- **`DRY_RUN=true` stays the default.** No code path should submit an
  application without this being deliberately overridden.
- **`prepare_application()` on `NaukriClient` stays `NotImplementedError`**
  until Stage 2 is explicitly approved (see Phase status below).
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
browser/      Playwright Naukri automation — Stage 1 (read-only) is done,
              Stage 2 (write ops) is NOT started
database/     SQLAlchemy models + repositories (upsert-pattern throughout)
cli/          `naukri-agent <command>` — see `doctor`, `inspect`
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
| 7 Stage 2 — write operations (resume refresh, apply prep) | ⛔ NOT started — needs explicit user approval before any code |

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

**Not yet captured:** the actual job-listing / apply-workflow page.
Every real run so far has been read-only Stage 1 (login → profile →
search → one listing). `APPLY_BUTTON` and `RESUME_SELECTION_CONTROLS`
in `selectors.py` are still `UNVERIFIED` placeholders. Before Stage 2
can be designed for real, a Stage 1 `naukri-agent inspect` run needs
to actually reach and capture a real job listing page.

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
change done. As of this handoff: 245 passed, 3 deselected.

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
