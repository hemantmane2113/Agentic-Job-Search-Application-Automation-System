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

import contextlib
import datetime
import json
import logging
import re
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from pydantic import BaseModel, Field

from naukri_agent.browser.pacing import pause
from naukri_agent.config import Settings
from naukri_agent.recommendations.employment import is_excluded

logger = logging.getLogger(__name__)

PARKED_NEEDS_HUMAN = "needs_human"

_clock = time.monotonic  # replaced in tests


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _replay_plan(old_questions: list, answers: list[str], fresh_questions: list) -> list[tuple[Any, str]] | None:
    """Pair each question on the REOPENED form with the answer the person already gave to the same question (matched by
    its text). None when the reopened form asks something the person has not answered, or an answer is no longer one of
    its options: then nothing is sent and the job is handed back. Fewer questions than before is fine: Naukri asks only
    what it needs."""
    by_text = {_norm(q.question_text): a for q, a in zip(old_questions, answers)}
    plan: list[tuple[Any, str]] = []
    for q in fresh_questions:
        answer = by_text.get(_norm(q.question_text))
        if answer is None:
            return None
        if q.options and answer not in q.options:
            match = next((o for o in q.options if _norm(o) == _norm(answer)), None)
            if match is None:
                return None
            answer = match
        plan.append((q, answer))
    return plan


MAX_LIVE_QUESTIONS = 15  # a form that asks more than this is not one to answer from a phone
_REOPEN_LIMIT = 2


class _LiveFormResult(BaseModel):
    texts: list[str] = Field(default_factory=list)
    answers: list[str] = Field(default_factory=list)
    detail: list[dict] = Field(default_factory=list)  # what was asked, how it was answered (for the evidence file)
    reopened: bool = False
    handback: str | None = None  # why nothing more was done and the job goes back to the person
    check_applied: bool = False  # the form (not the person) stopped things: look at Naukri's own page before handing back


def _live_suggestion(text: str, options: list[str], candidate: Any, experience: Any, answer_from_profile: Any) -> str | None:
    """The profile's answer to the question on screen, offered to the person as a suggestion only (never sent unasked)."""
    answer = answer_from_profile(text, candidate, experience).answer
    if not answer:
        return None
    if not options or answer in options:
        return answer
    return next((o for o in options if _norm(o) == _norm(answer)), None)


def _follow_live_form(
    client: Any, interaction: Any, settings: Settings, job: dict, candidate: Any, experience: Any, answer_from_profile: Any
) -> _LiveFormResult:
    """Answer the questions Naukri's form REALLY asks, one at a time, like a person applying by hand.

    Naukri's question list names every question the employer set, but its form asks only what it cannot fill from the profile
    or from earlier tries (23 Oct, Tiger Analytics: 7 listed, 1 asked), so answering the list typed answers into questions the
    form never asked, in a burst, and the form errored or lost answers. Here: read the question on screen, ask the person only
    that, send the answer only if the form is STILL asking that question, wait for Naukri's reply, repeat until the form
    closes. Anything unexpected hands the job back instead of guessing. Never raises for the expected problems."""
    from naukri_agent.browser.apply_workflow import FormChanged

    out = _LiveFormResult()
    saved: dict[str, str] = {}  # question text -> the answer the person already gave (kept across a reopened form)
    last_key: str | None = None
    repeats = reopens = 0
    use_saved = False
    first = True
    for _step in range(MAX_LIVE_QUESTIONS + 3 * _REOPEN_LIMIT + 3):
        state, live = client.read_live_question(first=first)
        first = False
        if state == "closed":
            return out  # Naukri has nothing more to ask: the caller checks that the job now shows as Applied
        if state != "question" or live is None:
            out.handback, out.check_applied = "Naukri's form is open but is not asking anything I can answer", True
            return out
        logger.info("live form: Naukri is asking %r (%s%s)", live.text[:140], live.kind,
                    f", {len(live.options)} choices" if live.options else "")  # the question only, never the answer
        key = _norm(live.text)
        repeats = repeats + 1 if key == last_key else 0
        last_key = key
        if repeats >= 2:
            out.handback, out.check_applied = f"Naukri keeps asking {live.text!r}; it did not accept the answer", True
            return out
        answer, source = (saved.get(key) if use_saved and repeats == 0 else None), "saved"
        if answer is None:
            if len(out.texts) >= MAX_LIVE_QUESTIONS:
                out.handback, out.check_applied = f"Naukri asked more than {MAX_LIVE_QUESTIONS} questions", True
                return out
            suggestion = _live_suggestion(live.text, live.options, candidate, experience, answer_from_profile)
            reply = interaction.ask_question(
                len(out.texts) + 1, 0, live.text, suggestion, options=list(live.options) or None
            )
            if reply is None:
                out.handback = "no answer was given on Telegram"
                return out
            if live.kind == "text":  # a typed answer cannot be taken back once sent, so confirm it
                ok = interaction.confirm_answer(live.text, reply)
                if not ok:
                    out.handback = "the typed answer was not confirmed" if ok is False else "no reply to the confirmation"
                    return out
            answer, source = reply, "telegram"
            saved[key] = answer
        use_saved = False
        try:
            status = client.answer_live_question(live, answer)
        except FormChanged as exc:
            if reopens >= _REOPEN_LIMIT:
                out.handback, out.check_applied = f"Naukri's form changed while waiting for the answer ({exc})", True
                return out
            reopens += 1
            interaction.notify("Naukri's form closed or moved on while you were answering, so I am reopening it and "
                               "sending your answer now - please wait.")
            client.reopen_application(job["url"])  # Naukri keeps the answers it already has, so this asks only what is left
            out.reopened = True
            use_saved, first, last_key, repeats = True, True, None, 0
            continue
        logger.info("live form: answer sent (%s); Naukri's form says: %s", source, status)
        out.texts.append(live.text)
        out.answers.append(answer)
        out.detail.append({"question": live.text, "kind": live.kind, "options": live.options, "answer": answer,
                           "source": source, "form_reply": status})
        if status == "closed":
            return out
    out.handback, out.check_applied = "Naukri's form did not finish after many steps", True
    return out


def _write_form_evidence(path: Any, questions: list, live: _LiveFormResult) -> None:
    """What Naukri's data listed versus what its form actually asked, so a surprise can be explained later. Local file only."""
    try:
        path.write_text(
            json.dumps(
                {
                    "listed_by_naukri": [
                        {"id": q.control_id, "text": q.question_text, "type": q.question_type, "skippable": q.skippable,
                         "options": q.options}
                        for q in questions
                    ],
                    "asked_by_the_form": live.detail,
                    "reopened": live.reopened,
                    "handed_back": live.handback,
                },
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - evidence must never fail an application
        pass


def _describe_unplaced(placed: Any) -> str:
    """What Naukri's profile showed when a resume upload could not be confirmed (file names only)."""
    shown = getattr(placed, "after_filename", None) or getattr(placed, "before_filename", None) or "no resume"
    return f"Naukri shows: {shown}; {getattr(placed, 'note', None) or 'no further note'}"


class AutoApplyOutcome(BaseModel):
    job_id: int
    title: str
    company: str
    url: str
    outcome: str  # applied | needs_human | unconfirmed | failed | not_native | excluded_type
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
    if name == "ApplyAnswerError":  # our own sentence (which option, which box), never page content or a secret
        return f"{name}: {str(exc)[:200]}"
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
    if o.outcome == "declined":
        return f"Skipped: {head} - you said no. Nothing was applied."
    if o.outcome == "not_native":
        return f"Skipped - this job no longer has a Naukri Apply button, so ignore the question above: {head}"
    if o.outcome == "excluded_type":
        return f"Skipped - Naukri lists this job as not full-time permanent ({o.detail}), so ignore the question above: {head}"
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
            cancel = getattr(interaction, "cancel_approval", None)
            if cancel is not None:
                cancel()  # this job is finished (applied, skipped, failed...): nothing more to hear on its card
            line = _status_line(item)
            if line:
                interaction.notify(line)
        return item

    def finish_applied(job: dict, attempt_id: str, notes: list[str], texts: list[str], answers: list[str]) -> None:
        """Naukri shows the job as Applied: record it in the history, its questions and answers, and tell the phone."""
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
                     + "; ".join(notes),
            )
            history_id = row.id
        record(job, attempt_id, "applied", "; ".join(notes), texts, answers)
        with session_scope(factory) as s:
            link_application_questions_to_history(s, attempt_id, history_id)
        result.applied += 1

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
    opener = open_client or _open_naukri_client
    session_armed = False  # an application form was opened in THIS browser session (the network guard is now armed)
    try:
        with contextlib.ExitStack() as stack:
            client = stack.enter_context(opener(settings))
            for job in candidates[:max_attempts]:
                job = {**job, "your_years": candidate.years_experience}
                if result.applied >= remaining:
                    result.stopped_reason = "daily cap reached"
                    break
                attempt_id = uuid.uuid4().hex
                texts: list[str] = []
                final_answers: list[str] = []
                clicked = False  # from the Apply click on, the application may have gone through
                if interaction is not None and job["job_id"] != pre_sent:
                    interaction.send_approval(job)  # asked while the page loads, not after
                try:
                    client.open_job_page(job["url"])
                    if client.detect_apply_type() != "native":
                        record(job, attempt_id, "not_native", "no Naukri Apply button on the page now")
                        continue
                    # Naukri's own Employment Type, read from the page that is open (older stored jobs may
                    # not have it yet). Contract / temporary / part-time jobs are never offered for applying.
                    live_type = client.read_employment_type()
                    if is_excluded(live_type, settings.employment_filter_enabled, title=job["title"]):
                        record(job, attempt_id, "excluded_type", f"Naukri says: {live_type}")
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
                        if session_armed:
                            # The network guard that protects the application form stays armed for the rest of a browser
                            # session, and it blocks the resume upload too (21 Oct: Eduitalent). A fresh browser has no guard.
                            logger.info("starting a fresh browser to change the profile resume (the guard is armed in this one)")
                            stack.close()
                            client = stack.enter_context(opener(settings))
                            session_armed = False
                        placed = client.ensure_profile_resume(job["resume_file"])
                        if not placed.verified:
                            why = _describe_unplaced(placed)
                            logger.warning("resume change not confirmed for job %s: %s", job["job_id"], why)
                            snapshot = getattr(client, "save_page_snapshot", None)
                            if snapshot is not None:  # what the profile page really showed, to read later
                                try:
                                    shots.mkdir(parents=True, exist_ok=True)
                                    snapshot(shots / f"{attempt_id}_resume")
                                except Exception:  # noqa: BLE001 - diagnostics never hide the real outcome
                                    pass
                            record(job, attempt_id, "failed",
                                   f"could not put the {job.get('resume_id')} resume on the profile ({why}); nothing was applied")
                            result.stopped_reason = "could not set the resume; stopped (nothing applied with a wrong resume)"
                            break
                        on_profile = job["resume_id"]
                        client.open_job_page(job["url"])  # the resume check left the job page
                    client.prepare_next_application()
                    pause(settings.apply_pause_min_seconds, settings.apply_pause_max_seconds)
                    clicked = True
                    session_armed = True
                    client.click_apply()
                    questions = client.list_questions()
                    opened_at = _clock()
                    reopened = False
                    live_done, live_reopened = False, False
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
                        if questions and settings.apply_follow_live_form and hasattr(client, "read_live_question"):
                            live = _follow_live_form(client, interaction, settings, job, candidate, experience, answer_from_profile)
                            shots.mkdir(parents=True, exist_ok=True)
                            _write_form_evidence(shots / f"{attempt_id}_form.json", questions, live)
                            texts, final_answers = live.texts, live.answers
                            if live.handback and live.check_applied:
                                # The form stopped asking (or kept asking) - but it may simply have finished. Naukri's own page
                                # decides; if it shows the job as Applied this is not a handback at all.
                                if client.confirm_application_after_answers(job["url"]).submitted:
                                    live.handback = None
                            if live.handback:
                                snapshot = getattr(client, "save_failure_snapshot", None)
                                if snapshot is not None:
                                    snapshot(shots / f"{attempt_id}_failure")
                                kept = "; the answers already sent are kept by Naukri, so applying by hand asks only what is left" if live.answers else ""
                                record(job, attempt_id, PARKED_NEEDS_HUMAN, live.handback + kept, texts, final_answers)
                                continue
                            live_done, live_reopened = True, live.reopened
                        else:
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

                    to_send = [] if live_done else list(zip(questions, final_answers))
                    waited = _clock() - opened_at
                    if (
                        interaction is not None and to_send and settings.apply_panel_max_wait_seconds
                        and waited > settings.apply_panel_max_wait_seconds
                    ):
                        # Naukri's form errors or closes if it sits open while the person answers on Telegram. Their answers
                        # are saved here, so open a fresh form and send them straight away.
                        logger.info("answers took %.0f s; reopening the application form before sending them", waited)
                        interaction.notify("Your answers are in. Naukri's form was open a while, so I am reopening it to "
                                           "send them now - please wait.")
                        fresh = client.reopen_application(job["url"])
                        plan = _replay_plan(questions, final_answers, fresh)
                        if plan is None:
                            record(job, attempt_id, PARKED_NEEDS_HUMAN,
                                   "Naukri's form asked different questions when it was reopened, so nothing was sent; "
                                   "please finish this one yourself", texts, final_answers)
                            continue
                        to_send, reopened = plan, True

                    unsent: list[str] = []
                    for position, (q, ans) in enumerate(to_send):
                        status = client.submit_answer(q.control_id or "", ans)
                        if status == "closed" and position < len(to_send) - 1:
                            # Naukri finished the form early (as with Aaum Research on 22 Oct): the rest were never asked.
                            unsent = [item[0].question_text for item in to_send[position + 1:]]
                            break

                    shots.mkdir(parents=True, exist_ok=True)
                    client.screenshot(shots / f"{attempt_id}_before_submit.png")
                    # Every flow seen so far submits by itself: the Apply click for a question-free
                    # job (jobs 352, 321) and the panel's Save for a choice question (374). None
                    # needed the guessed "submit" button, and for question-free jobs the Applied
                    # marker only shows after the job page is reloaded - the old 8s look-without-
                    # reloading logged two finished applications as "unconfirmed". So always
                    # confirm from Naukri's own page state (reloading once if needed).
                    submission = client.confirm_application_after_answers(job["url"])
                    skipped = list(getattr(client, "skipped_questions", lambda: [])())
                    if skipped:  # Naukri did not ask these, so nothing was sent for them; say so
                        submission.notes.append("Naukri did not ask: " + "; ".join(q[:80] for q in skipped))
                    if unsent:
                        submission.notes.append("Naukri finished the form before asking: " + "; ".join(q[:80] for q in unsent))
                    if reopened:
                        submission.notes.append(
                            f"answers were sent into a freshly reopened form (they took {waited:.0f} s to collect)"
                        )
                    if live_done:
                        if len(texts) != len(questions):
                            submission.notes.append(
                                f"Naukri's form asked {len(texts)} of the {len(questions)} questions in its list"
                            )
                        if live_reopened:
                            submission.notes.append("the form was reopened while you were answering; your answer went into the fresh form")
                    client.screenshot(shots / f"{attempt_id}_after_submit.png")

                    if not submission.submitted:
                        record(job, attempt_id, "unconfirmed", "; ".join(submission.notes), texts, final_answers)
                        result.stopped_reason = "an application could not be confirmed; stopped to be safe"
                        break

                    finish_applied(job, attempt_id, submission.notes, texts, final_answers)
                except Exception as exc:  # noqa: BLE001 - any surprise stops the whole run
                    detail = _describe_error(exc)
                    snapshot = getattr(client, "save_failure_snapshot", None)
                    if snapshot is not None:  # what the page looked like, so the real cause can be read later
                        try:
                            shots.mkdir(parents=True, exist_ok=True)
                            snapshot(shots / f"{attempt_id}_failure")
                        except Exception:  # noqa: BLE001 - never hide the real error behind a diagnostics problem
                            pass
                    recovered = None
                    if clicked:
                        # The application may have gone through even though typing an answer failed (Naukri can finish
                        # early). Ask Naukri's own page, the same check used after a normal submit; never click.
                        try:
                            recovered = client.confirm_application_after_answers(job["url"])
                        except Exception:  # noqa: BLE001
                            recovered = None
                    if recovered is not None and recovered.submitted:
                        finish_applied(
                            job, attempt_id,
                            [f"an error occurred while answering ({detail}), but Naukri's own page shows the job as Applied"]
                            + list(recovered.notes),
                            texts, final_answers,
                        )
                        continue
                    record(job, attempt_id, "failed", detail, texts, final_answers)
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
