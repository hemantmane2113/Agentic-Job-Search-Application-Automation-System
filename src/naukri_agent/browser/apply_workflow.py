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
from typing import Any
from urllib.parse import urlsplit

from naukri_agent.browser import selectors
from naukri_agent.browser.apply_inspection import (
    MutatingRequestBlocker,
    extract_application_ui,  # noqa: F401 - re-exported for callers that want a UI read alongside the question list
)
from naukri_agent.browser.models import ApplyQuestionPrompt, ApplySubmissionResult

logger = logging.getLogger(__name__)

# Mirrors apply_inspection._APPLY_INIT_PATH's value exactly — duplicated
# here as DATA, not logic, so that frozen module never needs to change
# to expose it. See this module's docstring.
_APPLY_INIT_PATH = "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"
_APPLY_INIT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({("POST", _APPLY_INIT_PATH)})

_LIST_QUESTIONS_POLL_COUNT = 6
_LIST_QUESTIONS_POLL_INTERVAL_MS = 500


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
            item.get("questionText")
            or item.get("question")
            or item.get("text")
            or item.get("label")
            or ""
        )
        if not qtext:
            continue
        prompts.append(
            ApplyQuestionPrompt(
                control_id=qid,
                question_text=str(qtext),
                skippable=qid in skippable_ids,
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

    def _ensure_installed(self) -> None:
        if self._installed:
            return
        self._blocker.install(self._page)
        self._blocker.arm()
        self._capture.install(self._page)
        self._installed = True

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
        return prompts

    def submit_answer(self, control_id: str, answer: str) -> None:
        self._page.fill(selectors.APPLY_ANSWER_INPUT, answer)
        self._page.click(selectors.APPLY_NEXT_BUTTON)

    def skip_question(self, control_id: str) -> None:
        self._page.click(selectors.APPLY_SKIP_BUTTON)

    def submit_application(self) -> ApplySubmissionResult:
        self._page.click(selectors.APPLY_FINAL_SUBMIT_BUTTON)
        return ApplySubmissionResult(
            submitted=True,
            notes=[
                "final submit control clicked; a real confirmation UI has never "
                "been observed — treat this result as provisional until verified "
                "against a live run"
            ],
        )
