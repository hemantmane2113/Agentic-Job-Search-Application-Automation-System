"""
Unattended auto-apply: the `naukri-agent auto-apply` command's logic.

Applies, with no human at the keyboard, to jobs that have a plain Naukri
"Apply" button -- and ONLY those. Jobs that say "Apply on company site" are
never touched here; they reach you in the daily email.

Hard rules, each enforced in code (and tested), not by convention:

  * Three switches must ALL be on: AUTO_APPLY=true, DRY_RUN=false and
    AUTO_APPLY_UNATTENDED=true. A pause file stops it regardless.
  * Only jobs the deterministic scorer put in AUTO_APPLY_DECISIONS (default
    ACCEPT) with a parsed extraction, still native, not applied, not tried
    before, and recently seen. At most AUTO_APPLY_DAILY_CAP per rolling 24h.
  * Screening questions are answered ONLY by agents/profile_answers.py
    (profile facts, no LLM). If any single question cannot be answered with
    certainty -- or the application screen shows input fields the app could
    not read -- NOTHING is submitted: the job is parked as `needs_human`, its
    questions are saved, and the summary email hands it to you.
  * An application counts as done only when the page confirms it. Anything
    else is `unconfirmed`, counts against the cap, and STOPS the whole run.
  * Any exception (login trouble, CAPTCHA/MFA, a changed page) stops the whole
    run. CAPTCHA/MFA are never bypassed.

Nothing here is reachable from run-daily/discover/the scheduler.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from pydantic import BaseModel, Field

from naukri_agent.config import Settings

logger = logging.getLogger(__name__)

PARKED_NEEDS_HUMAN = "needs_human"


class AutoApplyOutcome(BaseModel):
    job_id: int
    title: str
    company: str
    url: str
    outcome: str  # applied | needs_human | unconfirmed | failed | not_native
    detail: str = ""
    questions: list[str] = Field(default_factory=list)
    answers: list[str] = Field(default_factory=list)
    resume_id: str | None = None  # the resume this application went out with


class AutoApplyRunResult(BaseModel):
    blocked_reason: str | None = None  # a gate said no; nothing was opened
    stopped_reason: str | None = None  # the run ended early
    candidates_considered: int = 0
    applied: int = 0
    outcomes: list[AutoApplyOutcome] = Field(default_factory=list)
    email_status: str | None = None


from naukri_agent.recommendations.apply_ready import select_candidates, split_skill_factors  # noqa: E402,F401 - re-exported


def check_gates(settings: Settings, interactive: bool = False) -> str | None:
    """Why auto-apply must not run, or None if every gate is open.

    Unattended mode needs three switches. Interactive mode (a human approves
    every single job on Telegram) needs only the first two: the person IS the
    third gate."""
    missing = []
    if not settings.auto_apply:
        missing.append("AUTO_APPLY=true")
    if settings.dry_run:
        missing.append("DRY_RUN=false")
    if not interactive and not settings.auto_apply_unattended:
        missing.append("AUTO_APPLY_UNATTENDED=true")
    if missing:
        return f"auto-apply is off; it needs {', '.join(missing)} (all {2 if interactive else 3})"
    if settings.auto_apply_pause_file.exists():
        return f"paused: {settings.auto_apply_pause_file} exists (delete it to resume)"
    return None


@contextmanager
def _open_naukri_client(settings: Settings) -> Iterator[Any]:
    from naukri_agent.browser.browser_manager import BrowserManager
    from naukri_agent.browser.naukri_client import NaukriClient

    with BrowserManager(settings) as browser:
        client = NaukriClient(browser.page, settings)
        client.login()
        yield client


def _describe_error(exc: Exception) -> str:
    """Error type, plus the first line of the message for browser (Playwright) errors
    only: a bare "Error" says nothing, and those messages hold a page address, never
    a credential. Every other exception stays type-only, as before."""
    name = type(exc).__name__
    if type(exc).__module__.startswith("playwright"):
        first = (str(exc).splitlines() or [""])[0].strip()[:200]
        if first:
            return f"{name}: {first}"
    return name


def _status_line(o: AutoApplyOutcome) -> str | None:
    head = f"{o.title} - {o.company}"
    if o.outcome == "applied":
        sent = "".join(f"\nQ: {q}\nA: {a}" for q, a in zip(o.questions, o.answers))
        used = f"\nResume: {o.resume_id}" if o.resume_id else "\nResume: the one already on your profile (no role match)"
        return f"Applied: {head}{used}" + (f"\n\nAnswers sent:{sent}" if sent else "")
    if o.outcome == PARKED_NEEDS_HUMAN:
        return f"NOT submitted - please finish this one yourself:\n{head}\n{o.url}\nWhy: {o.detail}"
    if o.outcome == "unconfirmed":
        return f"Could not confirm this application - check Naukri before anything else:\n{head}\n{o.url}"
    if o.outcome == "failed":
        return f"An error stopped the run on: {head} ({o.detail}). Nothing further was done."
    if o.outcome == "not_native":
        return f"Skipped - this job no longer has a Naukri Apply button, so ignore the question above: {head}"
    return None


def _render_summary(result: AutoApplyRunResult) -> tuple[str, str]:
    applied = [o for o in result.outcomes if o.outcome == "applied"]
    parked = [o for o in result.outcomes if o.outcome == PARKED_NEEDS_HUMAN]
    problems = [o for o in result.outcomes if o.outcome in ("unconfirmed", "failed")]
    subject = (
        f"[naukri-agent] Auto-apply: {len(applied)} applied, "
        f"{len(parked)} need you, {len(problems)} problem(s)"
    )
    lines = ["Auto-apply summary", ""]
    if result.stopped_reason:
        lines += [f"Run stopped early: {result.stopped_reason}", ""]
    if applied:
        lines.append("APPLIED (Naukri confirmed):")
        for o in applied:
            lines.append(f"  - {o.title} - {o.company}\n    {o.url}")
            for q, a in zip(o.questions, o.answers):
                lines.append(f"      Q: {q}\n      A: {a}")
        lines.append("")
    if parked:
        lines.append("NEEDS YOU (not submitted - open the link and finish it yourself):")
        for o in parked:
            lines.append(f"  - {o.title} - {o.company}\n    {o.url}\n    why: {o.detail}")
            for q in o.questions:
                lines.append(f"      Q: {q}")
        lines.append("")
    if problems:
        lines.append("PROBLEMS - check these on Naukri before anything else:")
        for o in problems:
            lines.append(f"  - [{o.outcome}] {o.title} - {o.company}\n    {o.url}\n    {o.detail}")
        lines.append("")
    lines.append("Each application went out with the resume chosen for its role (put on your profile just before applying).")
    return subject, "\n".join(lines)


def run_auto_apply(
    settings: Settings,
    *,
    session_factory: Any = None,
    open_client: Callable[[Settings], Any] | None = None,
    now: datetime.datetime | None = None,
    send_summary: bool = True,
    interaction: Any = None,
    max_attempts: int | None = None,
) -> AutoApplyRunResult:
    """interaction=None: unattended (profile answers only, anything else parks the
    job). interaction=<TelegramInteraction-like>: a human approves each job,
    answers each question, and confirms before any submit; silence means no."""
    result = AutoApplyRunResult()
    reason = check_gates(settings, interactive=interaction is not None)
    if reason:
        result.blocked_reason = reason
        return result

    from naukri_agent.agents.profile_answers import answer_from_profile
    from naukri_agent.candidate.models import load_candidate_profile
    from naukri_agent.database.base import init_db, session_scope
    from naukri_agent.database.repositories import (
        add_application_question,
        add_auto_apply_attempt,
        auto_apply_count_since,
        link_application_questions_to_history,
        upsert_application_history,
        upsert_candidate,
    )
    from naukri_agent.matching.experience_matcher import build_experience_profile
    from naukri_agent.resume.models import load_master_resume

    now = now or datetime.datetime.now(datetime.UTC)
    factory = session_factory or init_db(settings)
    candidate = load_candidate_profile(settings.candidate_profile_path)
    experience = build_experience_profile(candidate, load_master_resume(settings.master_resume_path))

    with session_scope(factory) as session:
        cand_row = upsert_candidate(session, candidate)
        session.flush()
        candidate_id = cand_row.id
        remaining = settings.auto_apply_daily_cap - auto_apply_count_since(session, now - datetime.timedelta(hours=24))
        candidates = select_candidates(session, candidate_id, settings, now)
    result.candidates_considered = len(candidates)

    if remaining <= 0:
        result.stopped_reason = "daily cap already reached"
        return result
    if not candidates:
        result.stopped_reason = "no eligible jobs"
        return result

    def record(job: dict, attempt_id: str, outcome: str, detail: str, questions=(), answers=()) -> AutoApplyOutcome:
        item = AutoApplyOutcome(
            job_id=job["job_id"], title=job["title"], company=job["company"], url=job["url"],
            outcome=outcome, detail=detail, questions=list(questions), answers=list(answers),
            resume_id=job.get("resume_id"),
        )
        result.outcomes.append(item)
        with session_scope(factory) as s:
            add_auto_apply_attempt(
                s, job_id=job["job_id"], attempt_id=attempt_id, outcome=outcome,
                detail=json.dumps({"detail": detail, "questions": list(questions)})[:4000],
            )
            for i, q in enumerate(questions, start=1):
                ans = answers[i - 1] if i - 1 < len(answers) else None
                add_application_question(
                    s, job_id=job["job_id"], attempt_id=attempt_id, order_in_attempt=i,
                    question_text=q, final_answer=ans,
                )
        if interaction is not None:
            line = _status_line(item)
            if line:
                interaction.notify(line)
        return item

    shots = settings.inspection_output_dir / "auto_apply"
    max_attempts = max_attempts or settings.auto_apply_daily_cap * 3  # --max-jobs caps this for test runs

    # Interactive mode: put the FIRST job on the phone before the browser even starts.
    # Launching, logging in and loading the page take ~10-15s; the user can already be
    # reading and tapping meanwhile (Telegram keeps the reply until we read it).
    pre_sent = None
    if interaction is not None:
        first = {**candidates[0], "your_years": candidate.years_experience}
        interaction.send_approval(first)
        pre_sent = first["job_id"]
    on_profile: str | None = None  # resume id verified on the Naukri profile during this run
    try:
        with (open_client or _open_naukri_client)(settings) as client:
            for job in candidates[:max_attempts]:
                job = {**job, "your_years": candidate.years_experience}
                if result.applied >= remaining:
                    result.stopped_reason = "daily cap reached"
                    break
                attempt_id = uuid.uuid4().hex
                if interaction is not None and job["job_id"] != pre_sent:
                    interaction.send_approval(job)  # asked while the page loads, not after
                try:
                    client.open_job_page(job["url"])
                    if client.detect_apply_type() != "native":
                        record(job, attempt_id, "not_native", "no Naukri Apply button on the page now")
                        continue

                    if interaction is not None:
                        approved = interaction.wait_approval()
                        if approved is None:
                            record(job, attempt_id, "no_reply", "no reply on Telegram in time")
                            result.stopped_reason = "no reply on Telegram; stopped (silence is never a yes)"
                            break
                        if approved is False:
                            record(job, attempt_id, "declined", "you said no")
                            continue

                    # Naukri applies with the profile's CURRENT resume, so put the one chosen for this
                    # job's role there first (only after the Yes: a "no" never touches the profile).
                    # If it cannot be confirmed, nothing is applied - never the wrong resume.
                    if job.get("resume_file") and on_profile != job.get("resume_id"):
                        placed = client.ensure_profile_resume(job["resume_file"])
                        if not placed.verified:
                            record(job, attempt_id, "failed",
                                   f"could not put the {job.get('resume_id')} resume on the profile; nothing was applied")
                            result.stopped_reason = "could not set the resume; stopped (nothing applied with a wrong resume)"
                            break
                        on_profile = job["resume_id"]
                        client.open_job_page(job["url"])  # the resume check left the job page
                    client.prepare_next_application()
                    client.click_apply()
                    questions = client.list_questions()
                    fields = client.application_question_field_count() if not questions else 0

                    texts = [q.question_text for q in questions]
                    answers = [answer_from_profile(t, candidate, experience) for t in texts]
                    panel = bool(client.question_panel_open()) if not questions else False
                    unreadable = (
                        "a question panel opened but its questions could not be read"
                        if panel
                        else "the application screen shows input fields the app could not read"
                        if fields > 0
                        else "the application screen could not be read"
                    )
                    if interaction is None:
                        refused = [
                            (t, a.basis if a.answer is None else "the profile answer is not one of the offered options")
                            for q, t, a in zip(questions, texts, answers)
                            if a.answer is None or (q.options and a.answer not in q.options)
                        ]
                        if refused:
                            why = "; ".join(f"{t!r}: {b}" for t, b in refused)
                            record(job, attempt_id, PARKED_NEEDS_HUMAN, "cannot answer with certainty - " + why, texts)
                            continue
                        if not questions and (fields != 0 or panel):
                            record(job, attempt_id, PARKED_NEEDS_HUMAN, unreadable)
                            continue
                        final_answers = [a.answer or "" for a in answers]
                    else:
                        if not questions and (fields != 0 or panel):
                            record(job, attempt_id, PARKED_NEEDS_HUMAN, unreadable)
                            continue
                        final_answers = []
                        for i, (q, t, a) in enumerate(zip(questions, texts, answers), start=1):
                            reply = interaction.ask_question(i, len(texts), t, a.answer, options=list(q.options))
                            if reply is None:
                                break
                            final_answers.append(reply)
                        if len(final_answers) < len(texts):
                            record(job, attempt_id, PARKED_NEEDS_HUMAN,
                                   "no answer was given on Telegram", texts, final_answers)
                            continue
                        # A tap on a choice button IS the confirmation (the prompt says so), so
                        # no second Yes/No. Typed answers still get one: a typo is easy to make
                        # on a phone and impossible to take back.
                        if questions and not all(q.options for q in questions):
                            confirmed = interaction.confirm_submit(job, texts, final_answers)
                            if not confirmed:
                                record(job, attempt_id, "declined" if confirmed is False else "no_reply",
                                       "answers were not confirmed; nothing was submitted", texts, final_answers)
                                continue

                    for q, ans in zip(questions, final_answers):
                        client.submit_answer(q.control_id or "", ans)

                    shots.mkdir(parents=True, exist_ok=True)
                    client.screenshot(shots / f"{attempt_id}_before_submit.png")
                    # Every flow seen so far submits by itself: the Apply click for a question-free
                    # job (jobs 352, 321) and the panel's Save for a choice question (374). None
                    # needed the guessed "submit" button, and for question-free jobs the Applied
                    # marker only shows after the job page is reloaded - the old 8s look-without-
                    # reloading logged two finished applications as "unconfirmed". So always
                    # confirm from Naukri's own page state (reloading once if needed).
                    submission = client.confirm_application_after_answers(job["url"])
                    client.screenshot(shots / f"{attempt_id}_after_submit.png")

                    if not submission.submitted:
                        record(job, attempt_id, "unconfirmed", "; ".join(submission.notes), texts, final_answers)
                        result.stopped_reason = "an application could not be confirmed; stopped to be safe"
                        break

                    with session_scope(factory) as s:
                        row, _ = upsert_application_history(
                            s, job["job_id"],
                            source="agent_auto_apply_telegram" if interaction is not None else "agent_auto_apply_unattended",
                            resume_id=job.get("resume_id"),
                            resume_file_path=job.get("resume_file"),
                            resume_file_hash=job.get("resume_hash"),
                            note="Applied by naukri-agent auto-apply. "
                                 + (f"Resume {job['resume_id']} was put on the profile first. " if job.get("resume_id")
                                    else "No resume matched this role; the resume already on the profile was used. ")
                                 + "; ".join(submission.notes),
                        )
                        history_id = row.id
                    record(job, attempt_id, "applied", "; ".join(submission.notes), texts, final_answers)
                    with session_scope(factory) as s:
                        link_application_questions_to_history(s, attempt_id, history_id)
                    result.applied += 1
                except Exception as exc:  # noqa: BLE001 - any surprise stops the whole run
                    record(job, attempt_id, "failed", _describe_error(exc))
                    result.stopped_reason = f"stopped after an error ({type(exc).__name__})"
                    break
    except Exception as exc:  # noqa: BLE001 - browser/login failure before or between jobs
        result.stopped_reason = result.stopped_reason or f"could not run ({type(exc).__name__})"
        if interaction is not None and pre_sent is not None and not result.outcomes:
            interaction.notify(f"The run could not start ({type(exc).__name__}); please ignore the job question above.")

    if send_summary and (result.outcomes or result.stopped_reason):
        from naukri_agent.notifications.email import EmailMessage, build_email_sender
        from naukri_agent.notifications.exceptions import EmailSendError

        subject, body = _render_summary(result)
        try:
            build_email_sender(settings).send(EmailMessage(subject=subject, text_body=body))
            result.email_status = "sent"
        except EmailSendError as exc:
            result.email_status = f"send_failed: {type(exc).__name__}"
    return result
