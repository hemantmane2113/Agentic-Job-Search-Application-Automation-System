"""
Phase 14 apply-write workflow: the real (not frozen, not inspection-
only) module that clicks Apply, reads the live questionnaire, submits
human-approved answers, and performs the final submit.

browser/apply_inspection.py (Stage 1.5) stays byte-for-byte FROZEN per
CLAUDE.md — this module reuses its PATTERN, not its code, importing
exactly two names from it unmodified: MutatingRequestBlocker (the
default-deny network guard) and extract_application_ui (the generic
post-Apply DOM reader). Everything else here is new.

One literal has to be duplicated rather than shared, because the
frozen module's own copy is a private module constant:
_APPLY_INIT_PATH's value, re-declared below as DATA, not logic, so
apply_inspection.py never needs to change to expose it.

Unlike apply_inspection.py's deliberately sanitized, body-discarding
capture (that module is a logged, unattended inspection artifact and
must never persist real content), this module reads the REAL apply-init
response body — it is a live, human-supervised flow, and the actual
question text is exactly what the human needs to review. The precise
shape of that body has never been confirmed by a live run (only
sanitized top-level key NAMES were ever recorded by Stage 1.5); see
CLAUDE.md's Phase 14 open-risk notes. _parse_questionnaire() is
deliberately defensive and best-effort: an unrecognised shape returns
no questions rather than guessing, and notes why.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from naukri_agent.browser import selectors
from naukri_agent.browser.apply_inspection import (
    MutatingRequestBlocker,
    extract_application_ui,  # noqa: F401 - re-exported for callers that want a UI read alongside the question list
)
from naukri_agent.browser.apply_type import (  # noqa: F401 - detect_apply_type re-exported for callers/tests
    APPLY_TYPE_COMPANY_SITE,
    APPLY_TYPE_NATIVE,
    APPLY_TYPE_NONE,
    detect_apply_type,
    is_marked_applied,
)
from naukri_agent.browser.models import ApplyQuestionPrompt, ApplySubmissionResult

logger = logging.getLogger(__name__)

# Mirrors apply_inspection._APPLY_INIT_PATH's value exactly — duplicated
# here as DATA, not logic, so that frozen module never needs to change
# to expose it. See this module's docstring.
_APPLY_INIT_PATH = "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"
# Observed live 2026-10-08 (job 374): when the user picks an answer and the panel's
# Save is pressed, Naukri's chatbot sends the answer to this exact endpoint. With only
# the apply-init request allowed, the network guard BLOCKED it and the panel showed
# "Something went wrong", so no answer could ever be submitted. It is allowed by exact
# (method, path) like apply-init: the guard stays default-deny for everything else, and
# this request is only ever triggered by our own Save click, which the runner makes
# only AFTER the human has approved the job, answered, and confirmed.
_CHATBOT_RESPOND_PATH = "/cloudgateway-chatbot/chatbot-services/botapi/v5/respond"
_APPLY_INIT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {("POST", _APPLY_INIT_PATH), ("POST", _CHATBOT_RESPOND_PATH)}
)

_LIST_QUESTIONS_POLL_COUNT = 6
_LIST_QUESTIONS_POLL_INTERVAL_MS = 500
_APPLIED_POLL_COUNT = 12
_APPLIED_MARKER_WAIT_MS = 8000
_CHOICE_POLL_COUNT = 10
_FINAL_SUBMIT_CLICK_TIMEOUT_MS = 5000
_APPLIED_QUICK_WAIT_MS = 4000
_APPLIED_POLL_INTERVAL_MS = 1000
_APPLIED_TEXT_RE = re.compile(r"applied|application (has been )?(sent|submitted)", re.I)


class ApplyAnswerError(Exception):
    """An answer could not be entered into the question panel (nothing was submitted)."""


def _exact_path(raw: str) -> str:
    try:
        return urlsplit(raw or "").path
    except Exception:  # noqa: BLE001
        return ""


def _parse_questionnaire(apply_init_body: dict) -> tuple[list[ApplyQuestionPrompt], list[str]]:
    """
    Best-effort extraction of the question list from a real apply-init
    response body. Never raises; an unrecognised shape returns ([], notes)
    rather than guessing. See module docstring for why the exact shape
    is still unconfirmed.
    """
    notes: list[str] = []
    candidates: list[Any] = []

    if isinstance(apply_init_body.get("questionnaire"), list):
        candidates = apply_init_body["questionnaire"]
        notes.append("found at top-level 'questionnaire'")
    else:
        # The one real capture this project has (CLAUDE.md, 2026-09-09)
        # recorded these three as the only object-shaped top-level keys
        # — tried in order as the most likely nesting.
        for parent_key in ("chatbotResponse", "aurusFlow", "ncFlow"):
            parent = apply_init_body.get(parent_key)
            if isinstance(parent, dict) and isinstance(parent.get("questionnaire"), list):
                candidates = parent["questionnaire"]
                notes.append(f"found nested under {parent_key!r}.questionnaire")
                break

    jobs = apply_init_body.get("jobs")
    if not candidates and isinstance(jobs, list):
        # The REAL shape, captured live 2026-10-08 (job 374): jobs[].questionnaire[]
        lists = [j["questionnaire"] for j in jobs if isinstance(j, dict) and isinstance(j.get("questionnaire"), list)]
        if lists:
            candidates = [item for lst in lists for item in lst]
            notes.append("found under jobs[].questionnaire")
            if not candidates:
                notes.append("jobs[].questionnaire is empty: this application has no questions")
                return [], notes

    if not candidates:
        notes.append(
            "could not locate a question list in the apply-init response body; "
            "its real shape has never been confirmed by a live run"
        )
        return [], notes

    skippable_ids: set[str] = set()
    skippable_raw = apply_init_body.get("skippableQuestions")
    if isinstance(skippable_raw, list):
        for item in skippable_raw:
            if isinstance(item, str):
                skippable_ids.add(item)
            elif isinstance(item, dict):
                cid = item.get("id") or item.get("questionId") or item.get("controlId")
                if cid:
                    skippable_ids.add(str(cid))

    prompts: list[ApplyQuestionPrompt] = []
    for i, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or item.get("questionId") or item.get("controlId") or i)
        qtext = (
            item.get("questionName")
            or item.get("questionText")
            or item.get("question")
            or item.get("text")
            or item.get("label")
            or ""
        )
        if not qtext:
            continue
        raw_options = item.get("answerOption")
        options = (
            [str(v) for _k, v in sorted(raw_options.items()) if v not in (None, "")]
            if isinstance(raw_options, dict)
            else []
        )
        prompts.append(
            ApplyQuestionPrompt(
                control_id=qid,
                question_text=str(qtext),
                skippable=qid in skippable_ids or item.get("isMandatory") is False,
                options=options,
                question_type=(str(item["questionType"]) if item.get("questionType") else None),
            )
        )
    return prompts, notes


class _ApplyInitCapture:
    """
    Reads the REAL body of the one allowlisted apply-init response.
    Installed alongside (not instead of) MutatingRequestBlocker, via the
    same `response` event pattern apply_inspection.py's own observer
    uses (never route.fetch(), which would duplicate the real request —
    see that module's own documented history of this exact bug).
    Defensive: a lost/raced response (navigation, context close)
    degrades to a note, never a crash.
    """

    def __init__(self) -> None:
        self.body: dict | None = None
        self.notes: list[str] = []

    def install(self, target: Any) -> None:
        target.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        if self.body is not None:
            return  # only the first allowed apply-init response matters
        try:
            method = (response.request.method or "").upper()
            path = _exact_path(response.request.url or "")
        except Exception:  # noqa: BLE001 - a response we can't even identify is one we ignore
            return
        if (method, path) != ("POST", _APPLY_INIT_PATH):
            return
        try:
            raw = response.body()
            self.body = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - a lost/raced response degrades to a note, never crashes
            self.notes.append(f"apply-init response body unavailable: {type(exc).__name__}")


class ApplyWorkflowSession:
    """
    Owns apply-write state (the network guard + the apply-init response
    capture) for ONE application attempt on ONE NaukriClient. Created
    lazily by NaukriClient on first use of the apply-write surface;
    never constructed directly by callers outside browser/.
    """

    def __init__(self, page: Any) -> None:
        self._page = page
        self._blocker = MutatingRequestBlocker(allow_exact=_APPLY_INIT_ALLOWLIST)
        self._capture = _ApplyInitCapture()
        self._installed = False
        self._known: dict[str, ApplyQuestionPrompt] = {}

    def _ensure_installed(self) -> None:
        if self._installed:
            return
        self._blocker.install(self._page)
        self._blocker.arm()
        self._capture.install(self._page)
        self._installed = True

    def reset_capture(self) -> None:
        """Forget the previous job's apply-init response. The capture keeps only the
        FIRST response it sees, so a session that handles several jobs in a row
        would otherwise keep reading the first job's questions."""
        self._capture.body = None
        self._capture.notes = []
        self._known = {}

    def click_apply(self) -> None:
        self._ensure_installed()
        self._page.click(selectors.APPLY_BUTTON)

    def list_questions(self) -> list[ApplyQuestionPrompt]:
        for _ in range(_LIST_QUESTIONS_POLL_COUNT):
            if self._capture.body is not None:
                break
            try:
                self._page.wait_for_timeout(_LIST_QUESTIONS_POLL_INTERVAL_MS)
            except Exception:  # noqa: BLE001 - a page that can't wait just stops polling
                break
        if self._capture.body is None:
            return []
        prompts, notes = _parse_questionnaire(self._capture.body)
        for note in notes:
            logger.info("apply_workflow.list_questions: %s", note)
        self._known = {p.control_id: p for p in prompts if p.control_id}
        return prompts

    def submit_answer(self, control_id: str, answer: str) -> None:
        known = self._known.get(control_id)
        if known is not None and known.options:
            self._answer_choice(answer, known.options)
            return
        self._answer_text(answer)

    def _answer_text(self, answer: str) -> None:
        """Type a free-text / number answer into the question panel and press its Save. Both selectors are
        UNVERIFIED guesses (see selectors.py); when neither finds a box this raises ApplyAnswerError after
        ~11s instead of waiting 30s on one guess, so the failure is quick and says what happened."""
        last: Exception | None = None
        for selector, timeout in ((selectors.APPLY_DRAWER_TEXT_INPUT, 8000), (selectors.APPLY_ANSWER_INPUT, 3000)):
            try:
                self._page.fill(selector, answer, timeout=timeout)
                break
            except Exception as exc:  # noqa: BLE001 - try the next guess
                last = exc
        else:
            raise ApplyAnswerError("no text box for this question was found in the question panel") from last
        try:
            self._page.click(selectors.APPLY_DRAWER_SAVE, timeout=5000)  # the drawer's own Save (verified for choices)
        except Exception:  # noqa: BLE001 - the older guess
            self._page.click(selectors.APPLY_NEXT_BUTTON)

    def drawer_html(self) -> str | None:
        """The question drawer's HTML, for diagnosing a failure; None when there is no drawer or it cannot be read."""
        try:
            html = self._page.evaluate(
                "(sel) => { const e = document.querySelector(sel); return e ? e.outerHTML : null; }",
                selectors.APPLY_DRAWER,
            )
        except Exception:  # noqa: BLE001
            return None
        return str(html)[:300_000] if html else None

    def _answer_choice(self, answer: str, options: list[str]) -> None:
        """Pick one radio option in the question panel, then press its Save. Waits for
        the option to be on screen (a following question appears after the previous
        Save). Raises ApplyAnswerError, having clicked nothing, if the answer is not
        an offered option or never appears."""
        wanted = (answer or "").strip()
        if wanted not in options:
            raise ApplyAnswerError(f"{wanted!r} is not one of the offered options {options}")
        label = None
        for _ in range(_CHOICE_POLL_COUNT):
            for handle in self._page.query_selector_all(selectors.APPLY_CHOICE_LABEL):
                try:
                    if handle.is_visible() and (handle.inner_text() or "").strip() == wanted:
                        label = handle
                        break
                except Exception:  # noqa: BLE001 - a label mid-redraw is simply skipped
                    continue
            if label is not None:
                break
            self._page.wait_for_timeout(1000)
        if label is None:
            raise ApplyAnswerError(f"option {wanted!r} did not appear in the question panel")
        label.click()
        self._page.wait_for_timeout(400)  # Save is greyed out until an option is chosen
        self._page.click(selectors.APPLY_DRAWER_SAVE)

    def skip_question(self, control_id: str) -> None:
        self._page.click(selectors.APPLY_SKIP_BUTTON)

    def submit_application(self) -> ApplySubmissionResult:
        # Live 2026-10-08 (job 352): when a job has no screening questions the Apply
        # click ITSELF submits. Naukri then shows an "Applied" marker and there is no
        # further submit button, so hunting for one just timed out and a real, finished
        # application was logged as unconfirmed. Check for the marker first.
        if is_marked_applied(self._page, wait_ms=_APPLIED_MARKER_WAIT_MS):
            return ApplySubmissionResult(
                submitted=True,
                notes=["confirmed: Naukri shows the job as Applied (the Apply click itself submitted)"],
            )
        try:
            # no_wait_after: a successful submit navigates away (seen live on
            # 2026-10-08: to /myapply/saveApply). Playwright's default wait for
            # that navigation to settle timed out and crashed AFTER the
            # application had already been sent, so nothing got recorded.
            self._page.click(
                selectors.APPLY_FINAL_SUBMIT_BUTTON, no_wait_after=True, timeout=_FINAL_SUBMIT_CLICK_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001
            return ApplySubmissionResult(
                submitted=False,
                notes=[
                    f"final submit click did not complete ({type(exc).__name__}); "
                    "check Naukri before trying again"
                ],
            )
        signal = self._wait_for_applied_signal()
        if signal:
            return ApplySubmissionResult(submitted=True, notes=[f"confirmed: {signal}"])
        return ApplySubmissionResult(
            submitted=False,
            notes=["clicked, but no confirmation was seen; verify on Naukri before trying again"],
        )

    def confirm_applied_after_save(self, job_url: str) -> ApplySubmissionResult:
        """For choice (radio) questions the panel's Save IS the submit; there is no
        separate submit button to find. The live run that proved this lost ~36s waiting
        for one that does not exist, then logged a finished application as unconfirmed.
        Instead ask Naukri itself: is the job now marked Applied? Look at the page as
        it is; if not yet, reload the job page once (the panel closes and the page
        redraws) and look again. Never clicks anything."""
        if is_marked_applied(self._page, wait_ms=_APPLIED_QUICK_WAIT_MS):
            return ApplySubmissionResult(submitted=True, notes=["confirmed: Naukri shows the job as Applied"])
        try:
            self._page.goto(job_url)
        except Exception as exc:  # noqa: BLE001
            return ApplySubmissionResult(
                submitted=False,
                notes=[f"answer saved, but the job page could not be reloaded to check ({type(exc).__name__}); verify on Naukri"],
            )
        if is_marked_applied(self._page, wait_ms=_APPLIED_MARKER_WAIT_MS):
            return ApplySubmissionResult(
                submitted=True, notes=["confirmed: Naukri shows the job as Applied (after reloading the job page)"]
            )
        return ApplySubmissionResult(
            submitted=False,
            notes=["answer saved, but Naukri's page does not show Applied; verify on Naukri before trying again"],
        )

    def _wait_for_applied_signal(self) -> str | None:
        """Poll briefly for proof the application went through: the page moved to
        Naukri's /myapply/ result page, or the page text now says applied. Never raises."""
        for _ in range(_APPLIED_POLL_COUNT):
            try:
                if is_marked_applied(self._page):
                    return "page shows the Applied marker"
                url = self._page.url or ""
                if "/myapply/" in url:
                    return "page moved to Naukri's /myapply/ result page"
                text = self._page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                if _APPLIED_TEXT_RE.search(str(text)):
                    return "page text reports the job as applied"
                self._page.wait_for_timeout(_APPLIED_POLL_INTERVAL_MS)
            except Exception:  # noqa: BLE001 - mid-navigation reads can fail; keep polling
                try:
                    self._page.wait_for_timeout(_APPLIED_POLL_INTERVAL_MS)
                except Exception:  # noqa: BLE001
                    return None
        return None
