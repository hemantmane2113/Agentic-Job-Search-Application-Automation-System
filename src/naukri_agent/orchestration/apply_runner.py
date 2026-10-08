"""
Phase 14: run_apply_workflow — pipeline logic for the `apply` CLI
command. Same role orchestration/pipeline.py and orchestration/
discovery.py already play for run-daily/discover; NOT the "Orchestrator
class coordinating platforms" explicitly deferred until a second
platform exists — Click's dispatch remains the only cross-platform
coordinator for now.

Flow: resolve job + resume → open an isolated browser profile, log in
→ click Apply → read every question from the live apply-init response
→ draft an answer for every mandatory question (skippable ones are
just marked skipped, never drafted) → show the WHOLE batch to the
human in ONE review (not per-question) → on confirmation, submit each
approved answer, then require one more explicit "y" before the final,
irreversible submit.

Every ApplicationQuestion row is written in its own short transaction
immediately as it's known (not deferred to one final commit) — matching
apply_inspection.py's own "persist before a human pause" precedent — so
an aborted attempt still leaves a durable partial audit trail.

Never reachable from run-daily/discover/scheduler. See
tests/test_orchestration_apply_runner.py's structural guard asserting
the reverse: pipeline.py/discovery.py/scheduler/daemon.py never import
this module.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from pydantic import BaseModel

from naukri_agent.browser.models import ApplyQuestionPrompt
from naukri_agent.config import Settings

logger = logging.getLogger(__name__)


class ApplyUserInteraction:
    """
    Injectable human-interaction seam. The real CLI path uses this
    default implementation (real input()/print); tests supply their
    own canned object with the same two methods — no subclassing
    required, Python's duck typing is enough.
    """

    def __init__(
        self,
        input_fn: Callable[[str], str] | None = None,
        print_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._input = input_fn or input
        self._print = print_fn or print

    def confirm_answers(
        self, drafts: list[tuple[ApplyQuestionPrompt, str | None]]
    ) -> list[str]:
        """
        Show the WHOLE batch of mandatory questions + drafted answers at
        once (not one prompt per question) and return the final,
        human-approved answer for each, in the same order. A blank
        response accepts the drafted answer as-is; anything else
        replaces it. A question the LLM failed to draft (drafted is
        None) must be answered by the human from scratch.
        """
        if not drafts:
            return []
        self._print(
            "\n--- Review every drafted answer below before anything is typed "
            "into the application (press Enter to accept a drafted answer as-is) ---"
        )
        finals: list[str] = []
        for i, (question, drafted) in enumerate(drafts, start=1):
            self._print(f"\n[{i}] {question.question_text}")
            if drafted is not None:
                self._print(f"    Drafted: {drafted!r}")
            else:
                self._print("    (the LLM could not draft an answer — please write one)")
            edited = self._input("    Your answer: ")
            finals.append(edited.strip() or (drafted or ""))
        return finals

    def confirm_submission(self, summary: str) -> bool:
        """Print `summary` and require a literal 'y' before returning
        True — anything else means nothing is submitted."""
        self._print(summary)
        response = self._input(
            "\nSubmit this application? Type 'y' to confirm, anything else aborts: "
        )
        return response.strip() == "y"


class ApplyRunResult(BaseModel):
    job_id: int
    title: str
    company: str
    resume_id: str | None = None
    resume_file: str | None = None
    attempt_id: str
    questions_asked: int = 0
    questions_skipped: int = 0
    submitted: bool = False
    application_id: int | None = None
    aborted_reason: str | None = None


_NO_NATIVE_APPLY_REASONS = {
    "company_site": (
        "This listing only offers 'Apply on company site', which sends you to the "
        "employer's own website, so there is no Naukri application to automate. "
        "Nothing was clicked. Apply there yourself, then record it with "
        "`naukri-agent mark-applied`."
    ),
    "none": (
        "No Naukri Apply button was found on the job page (it may be expired, already "
        "applied, removed, or Naukri's layout changed). Nothing was clicked."
    ),
}


def _build_summary(
    title: str,
    company: str,
    resume_id: str | None,
    resume_file: str | None,
    rows: list[tuple[ApplyQuestionPrompt, str]],
) -> str:
    lines = [
        "\n--- Final summary before submission ---",
        f"Job: {title} @ {company}",
        f"Resume: {resume_id or 'none'} ({resume_file or 'no file selected'})",
    ]
    if rows:
        lines.append("Answers:")
        for question, answer in rows:
            lines.append(f"  Q: {question.question_text}\n  A: {answer}")
    else:
        lines.append("Answers: none (no question needed an answer)")
    return "\n".join(lines)


def run_apply_workflow(
    settings: Settings,
    job_ident: str,
    *,
    isolated_profile: bool = True,
    interaction: ApplyUserInteraction | None = None,
) -> ApplyRunResult:
    """
    Run one Phase 14 apply attempt end to end against the real site.
    Gated at the CLI layer (cli/main.py's `apply` command) by
    AUTO_APPLY=true AND DRY_RUN=false — this function itself does not
    re-check those, since it should also be directly callable from
    tests with a fake interaction.
    """
    if interaction is None:
        interaction = ApplyUserInteraction()

    # Imported lazily so importing this module (e.g. for the structural
    # guard test) never requires Playwright/a real DB — same pattern
    # BrowserManager.launch() and inspection.py already use.
    from naukri_agent.agents.apply_answer_agent import draft_application_answers
    from naukri_agent.browser.browser_manager import BrowserManager
    from naukri_agent.browser.naukri_client import NaukriClient
    from naukri_agent.candidate.models import load_candidate_profile
    from naukri_agent.database.base import init_db, session_scope
    from naukri_agent.database.repositories import (
        add_application_question,
        latest_resume_selection,
        link_application_questions_to_history,
        resolve_canonical_job,
        upsert_application_history,
    )
    from naukri_agent.llm.factory import get_apply_llm_provider
    from naukri_agent.resume.models import load_master_resume

    attempt_id = uuid.uuid4().hex
    factory = init_db(settings)

    with session_scope(factory) as session:
        job = resolve_canonical_job(session, job_ident)
        if job is None:
            return ApplyRunResult(
                job_id=-1, title="", company="", attempt_id=attempt_id,
                aborted_reason=f"No job matched {job_ident!r} (by id / external id / URL).",
            )
        job_id, job_title, company, job_url = job.id, job.title, job.company, job.url
        selection = latest_resume_selection(session, job_id)
        resume_id = selection.resume_id if selection else None
        resume_file = selection.file_path if selection else None

    candidate = load_candidate_profile(settings.candidate_profile_path)
    resume = load_master_resume(settings.master_resume_path)
    provider = get_apply_llm_provider(settings)
    provider_name = getattr(provider, "provider_name", None)

    profile_dir = None
    if isolated_profile:
        import datetime as _dt

        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        profile_dir = settings.inspection_output_dir / "_apply_session_profiles" / stamp

    skipped_count = 0
    # (order_in_attempt, question) for every mandatory question, so order
    # numbering survives the batch-review pass.
    mandatory: list[tuple[int, ApplyQuestionPrompt]] = []
    order = 0

    with BrowserManager(settings, profile_dir_override=profile_dir) as browser:
        client = NaukriClient(browser.page, settings)
        client.login()
        client.open_job_page(job_url)

        apply_type = client.detect_apply_type()
        if apply_type != "native":
            return ApplyRunResult(
                job_id=job_id, title=job_title, company=company, resume_id=resume_id,
                resume_file=resume_file, attempt_id=attempt_id,
                aborted_reason=_NO_NATIVE_APPLY_REASONS.get(apply_type, _NO_NATIVE_APPLY_REASONS["none"]),
            )

        client.click_apply()
        questions = client.list_questions()

        for question in questions:
            order += 1
            if question.skippable:
                client.skip_question(question.control_id or "")
                skipped_count += 1
                with session_scope(factory) as session:
                    add_application_question(
                        session, job_id=job_id, attempt_id=attempt_id,
                        order_in_attempt=order, question_text=question.question_text,
                        was_skipped=True,
                    )
                continue
            mandatory.append((order, question))

        # One batched LLM call for every mandatory question in this
        # application, instead of one call per question — the candidate/
        # resume grounding facts are identical across all of them, so
        # resending that context per question would be pure waste.
        drafts = draft_application_answers(
            provider, [q.question_text for _order, q in mandatory],
            candidate, resume, job_title, company,
        )
        pending_drafts: list[tuple[int, ApplyQuestionPrompt, str | None]] = [
            (order_in_attempt, q, draft.answer if draft else None)
            for (order_in_attempt, q), draft in zip(mandatory, drafts)
        ]

        review_input = [(q, drafted) for _order, q, drafted in pending_drafts]
        final_answers = interaction.confirm_answers(review_input)

        questions_asked = 0
        answered_rows: list[tuple[ApplyQuestionPrompt, str]] = []
        for (order_in_attempt, question, drafted_text), final_text in zip(
            pending_drafts, final_answers
        ):
            client.submit_answer(question.control_id or "", final_text)
            questions_asked += 1
            answered_rows.append((question, final_text))
            with session_scope(factory) as session:
                add_application_question(
                    session, job_id=job_id, attempt_id=attempt_id,
                    order_in_attempt=order_in_attempt, question_text=question.question_text,
                    drafted_answer=drafted_text, final_answer=final_text,
                    human_edited=(final_text != drafted_text),
                    llm_provider=provider_name, llm_model=provider.model,
                )

        summary = _build_summary(job_title, company, resume_id, resume_file, answered_rows)
        confirmed = interaction.confirm_submission(summary)

        if not confirmed:
            return ApplyRunResult(
                job_id=job_id, title=job_title, company=company, resume_id=resume_id,
                resume_file=resume_file, attempt_id=attempt_id,
                questions_asked=questions_asked, questions_skipped=skipped_count,
                submitted=False, aborted_reason="user declined final confirmation",
            )

        submission = client.submit_application()

    application_id: int | None = None
    if submission.submitted:
        with session_scope(factory) as session:
            app_row, _created = upsert_application_history(
                session, job_id, resume_id=resume_id, resume_file_path=resume_file,
                source="agent_auto_apply",
            )
            link_application_questions_to_history(session, attempt_id, app_row.id)
            application_id = app_row.id

    return ApplyRunResult(
        job_id=job_id, title=job_title, company=company, resume_id=resume_id,
        resume_file=resume_file, attempt_id=attempt_id, questions_asked=questions_asked,
        questions_skipped=skipped_count, submitted=submission.submitted,
        application_id=application_id,
    )
