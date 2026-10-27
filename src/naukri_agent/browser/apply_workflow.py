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
from naukri_agent.browser.models import ApplyQuestionPrompt, ApplySubmissionResult, LiveQuestion

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
# After an answer is saved, wait up to this long for Naukri's bot to ask the next question (or the form to close) before
# typing the next answer. Before this, four typed answers were sent in ONE second (23 Oct, Acme Analytics) and Naukri
# answered "There was an error while processing your job application".
_NEXT_QUESTION_WAIT_MS = 12000
_NEXT_QUESTION_POLL_MS = 300
# Reading the form's own question (see read_live_question). Right after Apply is clicked the form takes a moment to appear, so
# "gone" needs many looks in a row the first time (~5 s) and only two afterwards.
_LIVE_WAIT_MS = 15000
_FIRST_LOOK_ABSENT_POLLS = 16
# Bot messages that are not a question: Naukri's bot says these when it did not accept what was typed. The question it is
# waiting on is the last message that is NOT one of these (the saved panel of 2026-10-10 shows it replying "Sorry! I am not
# trained to answer this query :(" to an answer typed for a question it had not asked).
_BOT_NOISE_RE = re.compile(r"sorry|not trained|something went wrong|try again|did(?:n't| not) understand", re.I)
# One look at the open form: the bot's messages in order, the choice labels on screen, and whether the free-text box is showing
# (its container is hidden while a choice question is on screen). Returns null when there is no question drawer.
_LIVE_JS = """(a) => {
  const d = document.querySelector(a.drawer);
  if (!d) return null;
  const vis = (e) => !!(e && (e.offsetWidth || e.offsetHeight || (e.getClientRects && e.getClientRects().length)));
  const bots = Array.from(document.querySelectorAll(a.bot)).map((e) => (e.innerText || '').trim());
  const options = Array.from(document.querySelectorAll(a.choice)).filter(vis).map((e) => (e.innerText || '').trim());
  const box = document.querySelector(a.text);
  return { bots: bots, options: options, textVisible: vis(box) };
}"""
_FINAL_SUBMIT_CLICK_TIMEOUT_MS = 5000
_APPLIED_QUICK_WAIT_MS = 4000
_APPLIED_POLL_INTERVAL_MS = 1000
_APPLIED_TEXT_RE = re.compile(r"applied|application (has been )?(sent|submitted)", re.I)


class ApplyAnswerError(Exception):
    """An answer could not be entered into the question panel (nothing was submitted)."""


class FormChanged(ApplyAnswerError):
    """The form is no longer asking the question the answer was meant for (it closed, or moved on). Nothing was typed."""


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
        self.skipped: list[str] = []  # control ids of questions Naukri's panel never asked

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
        self.skipped = []

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

    def _live_snapshot(self) -> dict | None:
        try:
            got = self._page.evaluate(
                _LIVE_JS,
                {"drawer": selectors.APPLY_DRAWER, "bot": selectors.APPLY_BOT_ITEM,
                 "choice": selectors.APPLY_CHOICE_LABEL, "text": selectors.APPLY_DRAWER_TEXT_INPUT},
            )
        except Exception:  # noqa: BLE001 - mid-navigation, a closed page or a fake: no form to read
            return None
        return got if isinstance(got, dict) else None

    @staticmethod
    def _question_from(snapshot: dict) -> LiveQuestion | None:
        """The question that is waiting for an answer, or None when nothing is (the bot is still typing, or the form is done)."""
        bots = [str(t).strip() for t in snapshot.get("bots", []) if str(t).strip()]
        question = next((t for t in reversed(bots) if not _BOT_NOISE_RE.search(t)), None)
        if not question:
            return None
        options = [o for o in (str(x).strip() for x in snapshot.get("options", [])) if o]
        if options:
            return LiveQuestion(text=question, options=options, kind="choice")
        if snapshot.get("textVisible"):
            return LiveQuestion(text=question, options=[], kind="text")
        return None

    def read_live_question(self, wait_ms: int = _LIVE_WAIT_MS, first: bool = False) -> tuple[str, LiveQuestion | None]:
        """What is the form asking right now? Returns ('question', q) once the same question has been on screen on two looks
        in a row (so a new message never gets paired with the previous question's choices), ('closed', None) when the form is
        gone, or ('timeout', None) when it shows nothing answerable in time. `first=True` right after Apply was clicked.
        Never raises."""
        absent_needed = _FIRST_LOOK_ABSENT_POLLS if first else 2
        absent, waited = 0, 0
        previous: LiveQuestion | None = None
        while True:
            snap = self._live_snapshot()
            if snap is None:
                absent += 1
                previous = None
                if absent >= absent_needed:
                    return "closed", None
            else:
                absent = 0
                current = self._question_from(snap)
                if current is not None and current == previous:
                    return "question", current
                previous = current
            if waited >= wait_ms:
                # Nothing to answer: but a form that has finished may leave a "thank you" message instead of vanishing.
                if is_marked_applied(self._page, wait_ms=1000):
                    return "closed", None
                return "timeout", None
            try:
                self._page.wait_for_timeout(_NEXT_QUESTION_POLL_MS)
            except Exception:  # noqa: BLE001 - a page that cannot wait ends the look
                return "timeout", None
            waited += _NEXT_QUESTION_POLL_MS

    def answer_live_question(self, expected: LiveQuestion, answer: str) -> str:
        """Type or tap `answer` for `expected`, but ONLY if the form is still asking exactly that question (raises FormChanged,
        having typed nothing, if it closed or moved on), then wait for Naukri's reply: 'next' | 'closed' | 'timeout'."""
        state, now = self.read_live_question(wait_ms=3000)
        if state == "closed" or now is None and state != "timeout":
            raise FormChanged("Naukri's application form is no longer open")
        if state != "question" or now is None or self._norm(now.text) != self._norm(expected.text):
            raise FormChanged("Naukri is no longer asking that question")
        _present, bots_before = self._drawer_state()
        if now.kind == "choice":
            self._tap_option(answer, now.options)
        else:
            self._answer_text(answer)
        return self._wait_for_next_question(bots_before)

    def _tap_option(self, answer: str, options: list[str]) -> None:
        """Click the on-screen choice whose text is `answer`, then Save. Raises ApplyAnswerError, having clicked nothing, if
        it is not one of the choices on screen."""
        wanted = self._norm(answer)
        if wanted not in {self._norm(o) for o in options}:
            raise ApplyAnswerError(f"{answer!r} is not one of the choices on screen {options}")
        for handle in self._page.query_selector_all(selectors.APPLY_CHOICE_LABEL):
            try:
                if handle.is_visible() and self._norm(handle.inner_text()) == wanted:
                    handle.click()
                    self._page.wait_for_timeout(400)  # Save is greyed out until a choice is made
                    self._page.click(selectors.APPLY_DRAWER_SAVE)
                    return
            except ApplyAnswerError:
                raise
            except Exception:  # noqa: BLE001 - a label mid-redraw is skipped
                continue
        raise ApplyAnswerError(f"the choice {answer!r} could not be clicked")

    def _drawer_state(self) -> tuple[bool, int]:
        """(the question drawer is on screen, how many messages the bot has sent in it). (False, 0) when it cannot be read."""
        try:
            got = self._page.evaluate(
                "([drawer, bot]) => { const d = document.querySelector(drawer); "
                "return d ? [true, document.querySelectorAll(bot).length] : [false, 0]; }",
                [selectors.APPLY_DRAWER, selectors.APPLY_BOT_ITEM],
            )
            return bool(got[0]), int(got[1])
        except Exception:  # noqa: BLE001 - mid-navigation, a closed page or a fake: "cannot read" means "not there"
            return False, 0

    def _wait_for_next_question(self, bot_messages_before: int) -> str:
        """After an answer has been saved: 'next' once the bot has asked something new, 'closed' once the form has gone
        (Naukri finished, or skipped the rest), 'timeout' if neither happened in time. Never raises."""
        waited = 0
        absent = 0
        while waited < _NEXT_QUESTION_WAIT_MS:
            present, bots = self._drawer_state()
            if present:
                absent = 0
                if bots > bot_messages_before:
                    return "next"
            else:
                absent += 1
                if absent >= 2:  # gone on two looks in a row, not just redrawing
                    return "closed"
            try:
                self._page.wait_for_timeout(_NEXT_QUESTION_POLL_MS)
            except Exception:  # noqa: BLE001 - a page that cannot wait just stops the wait
                return "timeout"
            waited += _NEXT_QUESTION_POLL_MS
        return "timeout"

    def submit_answer(self, control_id: str, answer: str) -> str:
        """Enter ONE answer, then wait for Naukri to respond. Returns 'next', 'closed' (the form has finished or closed:
        stop answering and check whether Naukri shows the job as Applied), 'timeout', or 'skipped' (Naukri never asked this
        one and moved on)."""
        _present, bots_before = self._drawer_state()
        known = self._known.get(control_id)
        if known is not None and known.options:
            if not self._answer_choice(answer, known.options, control_id):
                return "skipped"
        else:
            self._answer_text(answer)
        return self._wait_for_next_question(bots_before)

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

    def page_html(self) -> str | None:
        """The whole page's HTML (capped), for diagnosing a failure when there is no question drawer to save."""
        try:
            return str(self._page.content())[:400_000]
        except Exception:  # noqa: BLE001
            return None

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

    def _visible_option_texts(self) -> list[str]:
        out: list[str] = []
        for handle in self._page.query_selector_all(selectors.APPLY_CHOICE_LABEL):
            try:
                if handle.is_visible():
                    out.append((handle.inner_text() or "").strip())
            except Exception:  # noqa: BLE001 - a label mid-redraw is simply left out
                continue
        return out

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join((text or "").split()).casefold()

    def _panel_is_asking_another_question(self, shown: list[str], control_id: str | None) -> bool:
        """True when every option on screen belongs to ANOTHER question the app has an answer for, which means Naukri
        moved past the one we are answering (it skips a question when an earlier answer already covered it)."""
        on_screen = {self._norm(t) for t in shown if t}
        if not on_screen:
            return False
        for cid, prompt in self._known.items():
            if cid == control_id or not prompt.options:
                continue
            if on_screen <= {self._norm(o) for o in prompt.options}:
                return True
        return False

    def _answer_choice(self, answer: str, options: list[str], control_id: str | None = None) -> bool:
        """Pick one option (radio or checkbox) in the question panel, then press its Save. Waits for the option to be on
        screen (a following question appears after the previous Save). Raises ApplyAnswerError, having clicked nothing,
        if the answer is not an offered option or never appears. If the panel is plainly asking a DIFFERENT question we
        have an answer for, Naukri skipped this one: it is left unanswered, noted, and the next question is tried."""
        wanted = (answer or "").strip()
        if wanted not in options:
            raise ApplyAnswerError(f"{wanted!r} is not one of the offered options {options}")
        label = None
        for _ in range(_CHOICE_POLL_COUNT):
            for handle in self._page.query_selector_all(selectors.APPLY_CHOICE_LABEL):
                try:
                    if handle.is_visible() and self._norm(handle.inner_text()) == self._norm(wanted):
                        label = handle
                        break
                except Exception:  # noqa: BLE001 - a label mid-redraw is simply skipped
                    continue
            if label is not None:
                break
            self._page.wait_for_timeout(1000)
        if label is None:
            if control_id is not None and self._panel_is_asking_another_question(self._visible_option_texts(), control_id):
                logger.warning("apply_workflow: Naukri did not ask question %s; moving on to the next one", control_id)
                self.skipped.append(control_id)
                return False
            raise ApplyAnswerError(f"option {wanted!r} did not appear in the question panel")
        label.click()
        self._page.wait_for_timeout(400)  # Save is greyed out until an option is chosen
        self._page.click(selectors.APPLY_DRAWER_SAVE)
        return True

    def skipped_question_texts(self) -> list[str]:
        return [self._known[c].question_text for c in self.skipped if c in self._known]

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
