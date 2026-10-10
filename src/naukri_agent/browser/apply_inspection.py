"""
Stage 1.5 apply-workflow inspection — a CONTROLLED, human-driven read
of Naukri's POST-Apply UI, which is created dynamically by JavaScript
and therefore cannot be seen in a pre-click DOM capture (see the
analysis of inspection_output/20260909_023839).

This is NOT Stage 2. It performs NO write operation:

  - the automation NEVER clicks the Apply button or any control inside
    the application UI;
  - the HUMAN clicks Apply, in the visible browser window;
  - a fail-safe, default-deny network guard (MutatingRequestBlocker)
    blocks every mutating request — POST / PUT / PATCH / DELETE, plus
    any non-idempotent or unrecognised method — for the whole rest of
    the session, so an accidental click cannot submit an application
    even if one happens;
  - the SOLE exception is one narrowly allowlisted request: the exact
    apply-initialisation POST observed in a real run
    (_APPLY_INIT_PATH), matched by exact (method, url-path) equality.
    It is allowed only so the post-Apply UI can render; its response
    METADATA (status / media type / body size / top-level JSON key
    names) is captured, never its body. Any submit/confirm endpoint —
    known or discovered later — stays blocked;
  - after the human confirms the post-Apply UI has rendered, the tool
    only READS it: full-page HTML, screenshot, the application root's
    outerHTML, and a structured enumeration of its inputs / labels /
    buttons — identifying (never operating) any resume-selection
    control and any final submit control.

Why the guard is armed AFTER login, not before
----------------------------------------------
Submitting Naukri's login form is itself a POST. A guard that blocked
mutating requests from the first byte would make login — automated OR
manual — impossible. So the guard is installed up front but left
DISARMED for the login phase only, during which there is no job page,
no Apply button, and no human interaction. It is ARMED before the human
is ever invited to touch the page, and there is no disarm(): it stays
active until the browser closes. Every mutating request that passes
through during the (fully automated) login phase is still recorded.

CAPTCHA / MFA
-------------
Handled by Stage 1's code, reused unchanged
(inspection._handle_login_challenge): if a challenge appears during
login the browser stays open and blocks for manual completion.

Secrets
-------
Never captured. The guard logs request METHOD + URL PATH + query
parameter KEY NAMES + POST body top-level KEY NAMES only — never
values, never cookies, never Authorization headers. Request headers are
never read at all.

For the ONE allowlisted apply-init response the guard additionally
records a SANITIZED recursive shape (_json_shape): object key NAMES,
array LENGTHS, primitive TYPES, string LENGTHS, and for URL-looking
strings only scheme/host/path. It never stores the raw body or string
contents; the only literal values it may hold are booleans and — for
the explicitly allowlisted enum-like keys in _SHAPE_SAFE_ENUM_KEYS — a
short safe-charset string or finite number. Depth/width/node budgets
keep the shape small.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from naukri_agent.browser import selectors
from naukri_agent.browser.browser_manager import BrowserManager
from naukri_agent.browser.exceptions import (
    NaukriAutomationError,
    NaukriCaptchaError,
    NaukriMfaError,
)
from naukri_agent.browser.inspection import (
    _handle_login_challenge,
    _record_failure,
    _save_snapshot,
)
from naukri_agent.browser.models import (
    AppInitResponseRecord,
    ApplyControl,
    ApplyInitRequestRecord,
    ApplyUiInspection,
    BlockedRequestRecord,
    ConsoleEventRecord,
    DialogInfo,
    DomMutationSummary,
    DomSampleRecord,
    FrameInfo,
    FrameLifecycleRecord,
    ObservedRequestRecord,
    PostInitEventRecord,
    WebSocketEventRecord,
)
from naukri_agent.browser.naukri_client import NaukriClient
from naukri_agent.config import Settings

logger = logging.getLogger(__name__)

# Methods that cannot create / modify / delete server-side state.
# Everything else — POST, PUT, PATCH, DELETE, and anything
# unrecognised — is blocked once the guard is armed (default-deny).
_IDEMPOTENT_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# --- The ONE narrowly-allowlisted apply-initialisation request. ---
# From a real inspect-apply run: clicking Apply fired exactly this POST,
# which the default-deny guard blocked (so no application UI rendered).
# Allowing it — and ONLY it — lets the workflow initialise so the
# post-Apply UI can be observed.
#
# Match rule (see MutatingRequestBlocker._handle):
#   * method == "POST"  AND
#   * urlsplit(url).path == this exact string (query/fragment stripped,
#     host ignored) — EXACT EQUALITY, never substring / prefix / regex,
#     so ".../v1/apply/submit", ".../v1/apply-confirm",
#     "/x/...apply-workflow/v1/apply" etc. are NOT matched and stay
#     blocked.
#   * only while the guard is ARMED (during the login phase every
#     mutating request already passes through and is logged).
# Everything else — other paths, other methods, any submit/confirm
# endpoint discovered later, analytics — remains default-denied.
_APPLY_INIT_PATH = "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"
_APPLY_INIT_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({("POST", _APPLY_INIT_PATH)})

# --- Post-init transition inspection (READ-ONLY) ---
# After the allowed Apply-init response, observe subsequent GET requests,
# non-apply-init responses, and frame navigations — sanitized metadata
# only. `_SAVE_APPLY_PATH` is the path `applyRedirectUrl` pointed at in a
# real capture (OBSERVED, not guessed); it is a watch target only and is
# NEVER allowlisted — a mutation to it is still default-denied.
_SAVE_APPLY_PATH = "/myapply/saveApply"
_POST_INIT_EVENT_MAX = 500
_POST_INIT_RESOURCE_TYPES: frozenset[str | None] = frozenset(
    {"document", "xhr", "fetch", "other", None}
)

# Plain-text (NOT CSS) hints, used only to CLASSIFY controls found
# inside the application UI for the design report. Never used to click.
_RESUME_CONTROL_HINTS = (
    "resume",
    "cv",
    "curriculum vitae",
    ".pdf",
    ".docx",
    "upload",
)
_FINAL_SUBMIT_HINTS = (
    "submit",
    "apply",
    "send application",
    "send",
    "confirm",
    "proceed",
    "continue",
    "finish",
)

# The instruction the human sees. The guard now lets exactly ONE
# request through (the apply-initialisation POST) so the UI can load;
# it still aborts every other mutating request, including any
# submit/confirm, so the post-Apply UI may look partial — hence
# "or has stopped loading/changing". No behavioural change: the tool
# still just waits for one Enter and clicks nothing.
_MANUAL_APPLY_PROMPT = (
    "\n[ACTION REQUIRED] The browser is on the test job page and the network guard "
    "is ARMED — the one apply-initialisation request is allowed so the workflow can "
    "start; every other application/submit request is blocked.\n\n"
    "Click Apply yourself. Do not answer questions or click any further "
    "application/submit controls. When the post-Apply UI has fully appeared "
    "(or has stopped loading/changing), return to the terminal and press Enter.\n\n"
    "Press Enter once the post-Apply UI has appeared or stopped changing: "
)

_MAX_OBSERVED_SAMPLE = 1000
_VISIBLE_TEXT_EXCERPT_LIMIT = 2000


# ---------------------------------------------------------------------------
# Secret-free request metadata helpers
# ---------------------------------------------------------------------------


def _safe_url(raw: str) -> tuple[str, list[str]]:
    """(scheme://host/path, [query-param key names]) — never any value."""
    parts = urlsplit(raw or "")
    if parts.scheme:
        base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    else:
        base = parts.path or (raw or "")
    keys = [k for k, _ in parse_qsl(parts.query, keep_blank_values=True)]
    return (base, keys)


def _exact_path(raw: str) -> str:
    """Just the path component, query/fragment stripped — the value the
    apply-init allowlist is matched against by EXACT equality."""
    try:
        return urlsplit(raw or "").path
    except Exception:  # noqa: BLE001
        return ""


def _safe_post_meta(request: Any) -> tuple[bool, int, list[str]]:
    """
    (present, size_bytes, [top-level key names]) for a request body.
    KEY NAMES ONLY — values are never read into any returned structure.
    """
    try:
        data = request.post_data
    except Exception:  # noqa: BLE001 - a body we can't read is one we don't log
        data = None
    if not data:
        return (False, 0, [])

    size = len(data.encode("utf-8", "ignore"))
    keys: list[str] = []
    try:
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            keys = sorted(str(k) for k in parsed)
    except Exception:  # noqa: BLE001 - not JSON; try form-encoding
        try:
            keys = sorted({k for k, _ in parse_qsl(data, keep_blank_values=True)})
        except Exception:  # noqa: BLE001
            keys = []
    return (True, size, keys)


# ---------------------------------------------------------------------------
# The network guard
# ---------------------------------------------------------------------------


class MutatingRequestBlocker:
    """
    Fail-safe, default-deny network guard for a Stage 1.5 apply
    inspection. Install once on a Playwright BrowserContext (preferred —
    also covers popups) or Page, BEFORE arming.

    Contract:
      * install(target): registers a single catch-all route. DISARMED.
      * arm(): from now on, any request whose method is not
        GET / HEAD / OPTIONS is aborted and recorded — UNLESS it is an
        exact match for an entry in `allow_exact` (see below).
        Idempotent. There is deliberately no disarm().
      * while disarmed, requests pass through untouched, but mutating
        ones are still recorded in `login_phase_passthrough`.
      * request headers are never read — no cookie / token / auth value
        is ever touched or logged.
      * the orchestrator never calls unroute(): the guard lives as long
        as the context/page does.

    allow_exact: a set of (METHOD, exact-path) pairs. A mutating request
    is allowed through ONLY when the guard is armed AND
    (request.method.upper(), urlsplit(request.url).path) is an exact
    member of this set — no substring / prefix / regex matching, so the
    allowlist cannot widen by accident.

    For an allowed request the guard records a REQUEST-side entry
    (`allowed_apply_init_requests`) and lets the browser make its own
    single real request (no route.fetch(), so no duplicate traffic and
    no fragile APIResponse lifecycle). The RESPONSE is observed
    separately via a `response` event listener (`_on_response`), which
    reads status / media-type / body-size and builds a sanitized shape
    from the `response` object alone — it never touches page/context, so
    a navigation or context close during the post-Apply flow degrades
    the capture to a note instead of crashing. Nothing sensitive is
    ever stored. Default allow_exact: empty (pure default-deny).

    POST-INIT TRANSITION INSPECTION (read-only). After the first allowed
    apply-init request, the guard also records — as sanitized metadata
    only — subsequent GET requests, non-apply-init responses, and frame
    navigations (`post_init_events`), and flags anything touching
    `_SAVE_APPLY_PATH` (`save_apply_events`). This observes; it never
    allows or blocks. A mutation to `_SAVE_APPLY_PATH` is still
    default-denied (and additionally flagged).
    """

    _APPLY_INIT_MAX = 50  # hard cap on request/response records (retry-loop guard)

    def __init__(self, allow_exact: "frozenset[tuple[str, str]] | None" = None) -> None:
        self._armed = False
        self._installed_on: Any = None
        self._allow_exact: frozenset[tuple[str, str]] = frozenset(
            (m.upper(), p) for m, p in (allow_exact or frozenset())
        )
        self.blocked: list[BlockedRequestRecord] = []
        self.observed_total = 0
        self.observed_sample: list[ObservedRequestRecord] = []
        self.login_phase_passthrough: list[ObservedRequestRecord] = []
        self.allowed_apply_init_requests: list[ApplyInitRequestRecord] = []
        self.apply_init_responses: list[AppInitResponseRecord] = []
        self.apply_init_records_capped = 0
        # -- post-init transition inspection (read-only) --
        self._seq = 0
        self._apply_init_marker_seq: int | None = None
        self._nav_observed = False
        self.post_init_events: list[PostInitEventRecord] = []
        self.save_apply_events: list[PostInitEventRecord] = []
        self.post_init_events_capped = 0

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def allow_exact(self) -> frozenset[tuple[str, str]]:
        return self._allow_exact

    @property
    def apply_init_marker_seq(self) -> int | None:
        return self._apply_init_marker_seq

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _post_init_active(self) -> bool:
        """True once the guard is armed AND the first Apply-init request
        has been seen — the window the transition inspection watches."""
        return self._armed and self._apply_init_marker_seq is not None

    def install(self, target: Any) -> None:
        if self._installed_on is not None:
            raise RuntimeError("MutatingRequestBlocker.install() called more than once")
        target.route("**/*", self._handle)
        # Response observation is event-based (not route.fetch()): it
        # reads only the `response` object, never page/context, so it
        # survives a post-Apply navigation / context close.
        try:
            target.on("response", self._on_response)
        except Exception as exc:  # noqa: BLE001 - a target without .on() still gets the guard
            logger.debug("MutatingRequestBlocker: target.on('response') unavailable: %s", exc)
        self._installed_on = target
        logger.info("MutatingRequestBlocker installed (disarmed) on %s", type(target).__name__)

    def observe_navigation(self, page: Any) -> None:
        """Register a `framenavigated` listener (page-level; a
        BrowserContext does not emit it) for the post-init transition
        inspection. Read-only, defensive, idempotent."""
        if self._nav_observed:
            return
        self._nav_observed = True
        try:
            page.on("framenavigated", self._on_framenavigated)
        except Exception as exc:  # noqa: BLE001
            logger.debug("observe_navigation: page.on('framenavigated') unavailable: %s", exc)

    def arm(self) -> None:
        self._armed = True
        logger.warning(
            "MutatingRequestBlocker ARMED — default-deny for every non-GET/HEAD/OPTIONS "
            "request is now in effect for the rest of this browser session (no disarm)."
        )

    # -- route handler -----------------------------------------------------

    def _handle(self, route: Any) -> None:
        request = route.request
        method = (getattr(request, "method", "") or "").upper()

        raw_url = getattr(request, "url", "") or ""
        try:
            base_url, query_keys = _safe_url(raw_url)
        except Exception:  # noqa: BLE001 - logging must never break the guard
            base_url, query_keys = "<unparseable>", []
        try:
            resource_type = getattr(request, "resource_type", None)
        except Exception:  # noqa: BLE001
            resource_type = None

        if method in _IDEMPOTENT_SAFE_METHODS:
            self.observed_total += 1
            if len(self.observed_sample) < _MAX_OBSERVED_SAMPLE:
                self.observed_sample.append(
                    ObservedRequestRecord(method=method, url=base_url, resource_type=resource_type)
                )
            self._maybe_record_post_init_get(request, base_url, query_keys, resource_type, raw_url)
            _continue(route)
            return

        # --- mutating (or unknown) method ---
        if not self._armed:
            # Login phase only. Allow, but record.
            self.login_phase_passthrough.append(
                ObservedRequestRecord(method=method, url=base_url, resource_type=resource_type)
            )
            logger.info(
                "Login-phase mutating request allowed (guard not yet armed): %s %s",
                method,
                base_url,
            )
            _continue(route)
            return

        # --- armed + mutating: the ONE narrow allowlist, exact match only ---
        if self._allow_exact and (method, _exact_path(raw_url)) in self._allow_exact:
            self._record_allowed_request(request, method, base_url, resource_type)
            _continue(route)  # browser makes its own single real request
            return

        try:
            is_nav = bool(request.is_navigation_request())
        except Exception:  # noqa: BLE001
            is_nav = False
        post_present, post_size, post_keys = _safe_post_meta(request)

        self.blocked.append(
            BlockedRequestRecord(
                method=method,
                url=base_url,
                query_param_keys=query_keys,
                resource_type=resource_type,
                is_navigation=is_nav,
                post_data_present=post_present,
                post_data_size_bytes=post_size,
                post_data_top_level_keys=post_keys,
            )
        )
        logger.warning("BLOCKED mutating request (default-deny): %s %s", method, base_url)
        # Still blocked — but if it targets the observed post-init redirect
        # path, ALSO flag it for the transition report (req: track anything
        # that requests /myapply/saveApply, allowed or not).
        if self._post_init_active() and _exact_path(raw_url) == _SAVE_APPLY_PATH:
            self._record_post_init_event(
                kind="blocked-mutation",
                method=method,
                base_url=base_url,
                query_keys=query_keys,
                resource_type=resource_type,
                is_navigation=is_nav,
                is_save_apply=True,
            )
        _abort(route)

    # -- narrow allowlist: apply-initialisation request only --------------

    def _record_allowed_request(
        self, request: Any, method: str, base_url: str, resource_type: str | None
    ) -> None:
        """Request-side record for the ONE allowlisted apply-init POST.
        The browser then makes its own single real request (caller
        `_continue`s the route). Response capture is `_on_response`."""
        if len(self.allowed_apply_init_requests) >= self._APPLY_INIT_MAX:
            self.apply_init_records_capped += 1
            return
        try:
            is_nav = bool(request.is_navigation_request())
        except Exception:  # noqa: BLE001
            is_nav = False
        _present, post_size, post_keys = _safe_post_meta(request)
        seq = self._next_seq()
        if self._apply_init_marker_seq is None:
            self._apply_init_marker_seq = seq  # anchor for post-init correlation
        self.allowed_apply_init_requests.append(
            ApplyInitRequestRecord(
                request_index=len(self.allowed_apply_init_requests) + 1,
                seq=seq,
                method=method,
                url=base_url,
                resource_type=resource_type,
                is_navigation=is_nav,
                post_data_size_bytes=post_size,
                post_data_top_level_keys=post_keys,
            )
        )
        logger.warning(
            "ALLOWED apply-init request (exact allowlist match): %s %s", method, base_url
        )

    # -- post-init transition inspection (read-only) ---------------------

    def _record_post_init_event(
        self,
        *,
        kind: str,
        base_url: str,
        method: str | None = None,
        query_keys: list[str] | None = None,
        resource_type: str | None = None,
        is_navigation: bool = False,
        status: int | None = None,
        content_type: str | None = None,
        is_save_apply: bool = False,
        note: str | None = None,
    ) -> None:
        if len(self.post_init_events) >= _POST_INIT_EVENT_MAX and not is_save_apply:
            self.post_init_events_capped += 1
            return
        rec = PostInitEventRecord(
            seq=self._next_seq(),
            kind=kind,
            method=method,
            url=base_url,
            query_param_keys=list(query_keys or []),
            resource_type=resource_type,
            is_navigation=is_navigation,
            status=status,
            content_type=content_type,
            is_save_apply=is_save_apply,
            note=note,
        )
        self.post_init_events.append(rec)
        if is_save_apply:
            self.save_apply_events.append(rec)

    def _maybe_record_post_init_get(
        self,
        request: Any,
        base_url: str,
        query_keys: list[str],
        resource_type: str | None,
        raw_url: str,
    ) -> None:
        if not self._post_init_active():
            return
        is_save = _exact_path(raw_url) == _SAVE_APPLY_PATH
        if resource_type not in _POST_INIT_RESOURCE_TYPES and not is_save:
            return
        try:
            is_nav = bool(request.is_navigation_request())
        except Exception:  # noqa: BLE001
            is_nav = False
        self._record_post_init_event(
            kind="get-request",
            method=(getattr(request, "method", "") or "").upper() or "GET",
            base_url=base_url,
            query_keys=query_keys,
            resource_type=resource_type,
            is_navigation=is_nav,
            is_save_apply=is_save,
        )

    def _maybe_record_post_init_response(
        self, response: Any, request: Any, method: str, base_url: str, query_keys: list[str], raw_url: str
    ) -> None:
        if not self._post_init_active():
            return
        is_save = _exact_path(raw_url) == _SAVE_APPLY_PATH
        try:
            resource_type = getattr(request, "resource_type", None)
        except Exception:  # noqa: BLE001
            resource_type = None
        if resource_type not in _POST_INIT_RESOURCE_TYPES and not is_save:
            return
        note: str | None = None
        try:
            status = getattr(response, "status", None)
        except Exception:  # noqa: BLE001
            status = None
            note = _join_note(note, "status unavailable")
        try:
            content_type = _media_type(response.headers.get("content-type"))
        except Exception:  # noqa: BLE001
            content_type = None
            note = _join_note(note, "content-type unavailable")
        self._record_post_init_event(
            kind="response",
            method=method or None,
            base_url=base_url,
            query_keys=query_keys,
            resource_type=resource_type,
            status=status,
            content_type=content_type,
            is_save_apply=is_save,
            note=note,
        )

    def _on_framenavigated(self, frame: Any) -> None:
        """Read-only observer for frame navigations after Apply-init.
        Touches only `frame.url`; never raises out."""
        try:
            if not self._post_init_active():
                return
            try:
                raw_url = getattr(frame, "url", "") or ""
            except Exception as exc:  # noqa: BLE001 - navigation / context close race
                self._record_post_init_event(
                    kind="navigation",
                    base_url="<unavailable>",
                    is_navigation=True,
                    note=f"frame.url unavailable: {type(exc).__name__}",
                )
                return
            if not raw_url or raw_url == "about:blank":
                return
            base_url, query_keys = _safe_url(raw_url)
            self._record_post_init_event(
                kind="navigation",
                base_url=base_url,
                query_keys=query_keys,
                is_navigation=True,
                is_save_apply=_exact_path(raw_url) == _SAVE_APPLY_PATH,
            )
        except Exception as exc:  # noqa: BLE001 - a nav observer must never break the run
            logger.debug("_on_framenavigated swallowed %s: %s", type(exc).__name__, exc)

    def _on_response(self, response: Any) -> None:
        """
        Response observer. For the ONE allowlisted apply-init POST:
        capture status / media-type / body-size / sanitized JSON shape.
        For every OTHER response after Apply-init: record sanitized
        metadata only (method / normalized url / query NAMES / status /
        media type) for the post-init transition inspection. Reads ONLY
        the `response` object and its `request` — never page/context —
        so a navigation or context close degrades to a note, never a
        raise. Never raises out.
        """
        try:
            request = getattr(response, "request", None)
            method = (getattr(request, "method", "") or "").upper()
            raw_url = getattr(request, "url", "") or ""
            if not self._armed:
                return
            base_url, query_keys = _safe_url(raw_url)

            if not (self._allow_exact and (method, _exact_path(raw_url)) in self._allow_exact):
                self._maybe_record_post_init_response(
                    response, request, method, base_url, query_keys, raw_url
                )
                return
            if len(self.apply_init_responses) >= self._APPLY_INIT_MAX:
                self.apply_init_records_capped += 1
                return

            request_index = len(self.apply_init_responses) + 1
            note: str | None = None

            try:
                status = getattr(response, "status", None)
            except Exception:  # noqa: BLE001
                status = None
                note = _join_note(note, "status unavailable")
            try:
                content_type = _media_type(response.headers.get("content-type"))
            except Exception:  # noqa: BLE001
                content_type = None
                note = _join_note(note, "content-type unavailable")

            body: bytes | None = None
            try:
                body = response.body()
            except Exception as exc:  # noqa: BLE001 - navigation / context close race
                note = _join_note(note, f"body unavailable: {type(exc).__name__}")

            body_size: int | None = None
            json_keys: list[str] = []
            json_shape: dict | None = None
            json_shape_truncated = False
            if body is not None:
                body_size = len(body)
                if body and content_type and "json" in content_type.lower():
                    try:
                        parsed = json.loads(body)
                    except Exception:  # noqa: BLE001
                        parsed = _UNSET
                        note = _join_note(note, "content-type is JSON but body did not parse")
                    if parsed is not _UNSET:
                        if isinstance(parsed, dict):
                            json_keys = sorted(str(k) for k in parsed)
                        else:
                            note = _join_note(note, "JSON response top level is not an object")
                        try:
                            json_shape, json_shape_truncated = _json_shape_root(parsed)
                        except Exception as exc:  # noqa: BLE001
                            note = _join_note(note, f"shape build failed: {type(exc).__name__}")
                # `parsed` / `body` go out of scope here — never stored.

            self.apply_init_responses.append(
                AppInitResponseRecord(
                    request_index=request_index,
                    seq=self._next_seq(),
                    method=method,
                    url=base_url,
                    status=status,
                    content_type=content_type,
                    body_size_bytes=body_size,
                    json_top_level_keys=json_keys,
                    json_shape=json_shape,
                    json_shape_truncated=json_shape_truncated,
                    note=note,
                )
            )
            logger.warning(
                "OBSERVED apply-init response: %s %s -> HTTP %s (%s)",
                method,
                base_url,
                status,
                content_type,
            )
        except Exception as exc:  # noqa: BLE001 - a response observer must never break the run
            logger.debug("_on_response swallowed %s: %s", type(exc).__name__, exc)


def _media_type(content_type_header: str | None) -> str | None:
    if not content_type_header:
        return None
    return content_type_header.split(";")[0].strip() or None


_UNSET = object()


def _join_note(existing: str | None, addition: str) -> str:
    return addition if not existing else f"{existing}; {addition}"


# ---------------------------------------------------------------------------
# Sanitized recursive JSON shape (for the allowlisted apply-init response
# ONLY). Captures structure — key NAMES, array LENGTHS, primitive TYPES,
# string LENGTHS, URL scheme/host/path — never the body, never string
# contents, never values (except booleans and an explicitly allowlisted
# set of enum-like keys). Bounded so a report can never grow huge.
# ---------------------------------------------------------------------------

_SHAPE_MAX_DEPTH = 6
_SHAPE_MAX_KEYS = 50  # per object
_SHAPE_MAX_ARRAY_ITEMS = 5  # element shapes sampled per array
_SHAPE_NODE_BUDGET = 800  # total shape nodes across the whole tree

# The ONLY keys whose literal (non-boolean) value may be recorded, and
# only then if the value is a finite number or a short safe-charset
# string. Everything else is type/length only.
_SHAPE_SAFE_ENUM_KEYS = frozenset({"statuscode", "flowtype"})
_SHAPE_ENUM_VALUE_MAXLEN = 24
_SHAPE_ENUM_VALUE_RE = re.compile(r"[A-Za-z0-9_-]+")


class _ShapeBudget:
    def __init__(self, total: int) -> None:
        self.remaining = total
        self.truncated = False

    def take(self) -> bool:
        if self.remaining <= 0:
            self.truncated = True
            return False
        self.remaining -= 1
        return True


def _looks_like_url(s: str) -> bool:
    if not s or len(s) > 2048 or " " in s or "\n" in s or "\t" in s:
        return False
    try:
        parts = urlsplit(s)
    except Exception:  # noqa: BLE001
        return False
    if parts.scheme in ("http", "https"):
        return True
    return s.startswith("/") and len(parts.path) > 1


def _url_shape(s: str) -> dict:
    """scheme / host / path ONLY — query and fragment are dropped."""
    parts = urlsplit(s)
    return {
        "type": "url",
        "length": len(s),
        "scheme": parts.scheme or None,
        "host": parts.netloc or None,
        "path": parts.path or None,
    }


def _safe_enum_value(key: str | None, value: Any) -> Any:
    if isinstance(value, bool):
        return value  # a bool is never a secret
    if not key or key.lower() not in _SHAPE_SAFE_ENUM_KEYS:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        if len(value) <= _SHAPE_ENUM_VALUE_MAXLEN and _SHAPE_ENUM_VALUE_RE.fullmatch(value):
            return value
    return None


def _json_shape(value: Any, budget: _ShapeBudget, depth: int, key: str | None = None) -> dict:
    if not budget.take():
        return {"type": "truncated", "reason": "node_budget"}
    if depth > _SHAPE_MAX_DEPTH:
        budget.truncated = True
        return {"type": "truncated", "reason": "max_depth"}

    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, int):
        node: dict = {"type": "integer"}
        ev = _safe_enum_value(key, value)
        if ev is not None:
            node["value"] = ev
        return node
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        if _looks_like_url(value):
            return _url_shape(value)
        node = {"type": "string", "length": len(value)}
        ev = _safe_enum_value(key, value)
        if ev is not None:
            node["value"] = ev
        return node
    if isinstance(value, list):
        node = {"type": "array", "length": len(value)}
        items = [
            _json_shape(item, budget, depth + 1, key=None)
            for item in value[:_SHAPE_MAX_ARRAY_ITEMS]
        ]
        node["items"] = items
        if len(value) > _SHAPE_MAX_ARRAY_ITEMS:
            node["items_sampled"] = _SHAPE_MAX_ARRAY_ITEMS
        return node
    if isinstance(value, dict):
        node = {"type": "object", "key_count": len(value)}
        keys: dict[str, dict] = {}
        for i, (k, v) in enumerate(sorted(value.items(), key=lambda kv: str(kv[0]))):
            if i >= _SHAPE_MAX_KEYS:
                node["keys_omitted"] = len(value) - _SHAPE_MAX_KEYS
                budget.truncated = True
                break
            keys[str(k)] = _json_shape(v, budget, depth + 1, key=str(k))
        node["keys"] = keys
        return node
    return {"type": "unknown"}


def _json_shape_root(parsed: Any) -> tuple[dict, bool]:
    budget = _ShapeBudget(_SHAPE_NODE_BUDGET)
    shape = _json_shape(parsed, budget, depth=0, key=None)
    return shape, budget.truncated


def _continue(route: Any) -> None:
    try:
        route.continue_()
    except Exception as exc:  # noqa: BLE001
        logger.debug("route.continue_() raised %s; ignoring", exc)


def _abort(route: Any) -> None:
    # Fail CLOSED: if abort itself fails we log and stop — never fall
    # back to continue_(), which would defeat the guard.
    try:
        route.abort("blockedbyclient")
    except Exception as exc:  # noqa: BLE001
        logger.error("route.abort() raised %s — request left hanging (fail-closed)", exc)


# ---------------------------------------------------------------------------
# Post-init client-workflow observation (READ-ONLY, PASSIVE)
#
# Why the questionnaire returned by Apply-init does not visibly render.
# Passive listeners (console / pageerror / websocket / frame lifecycle)
# + an injected AGGREGATE MutationObserver + bounded repeated re-runs of
# the EXISTING extract_application_ui() classification. Metadata only.
# Never allows/blocks a request; never clicks/fills/focuses/dispatches.
# ---------------------------------------------------------------------------

_CLIENT_OBS_MAX = 200  # hard cap per observation collection
_DOM_SAMPLE_COUNT = 6  # bounded re-samples after the human presses Enter
_DOM_SAMPLE_INTERVAL_MS = 750  # -> ~4.5s bounded post-Enter sampling window
_CONSOLE_EXCERPT_MAX = 300  # length cap on a redacted error/warning first line

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s'\"()<>]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TOKENISH_RE = re.compile(
    r"(?i)("
    r"bearer\s+\S+"  # Bearer <token>
    r"|(?:token|secret|cookie|set-cookie|authorization|auth|sid|session[_-]?id|session"
    r"|password|passwd|pwd|api[_-]?key|apikey|jwt|access[_-]?token|refresh[_-]?token)"
    r"\s*[=:]\s*\S+"  # k=v / k: v secrets
    r"|[A-Za-z0-9_\-]{10,}(?:\.[A-Za-z0-9_\-]{6,}){1,2}"  # dotted tokens / JWT
    r"|[A-Za-z0-9_\-]{20,}"  # long opaque blobs
    r")"
)

# Injected once (add_init_script -> every document; plus a best-effort
# evaluate for the current one). Buffers AGGREGATE mutation counts into a
# page global; drained by PostInitClientObserver. Reads only tag names,
# element ids and counts — never text, attribute values, or HTML.
_MUTATION_OBSERVER_SCRIPT = """
(() => {
  if (window.__naukriObs) return;
  const s = {added:0, removed:0, attrs:0, chardata:0, addTags:{}, remTags:{}, locs:{}, t0: Date.now()};
  const bump = (o,k) => { o[k] = (o[k]||0) + 1; };
  const loc = (n) => {
    try {
      let el = (n && n.nodeType === 1) ? n : (n && n.parentElement);
      let hops = 0;
      while (el && el !== document.documentElement && hops < 40) {
        if (el.id) return el.tagName.toLowerCase() + '#' + el.id;
        if (el === document.body) return 'body';
        el = el.parentElement; hops++;
      }
      return 'documentElement';
    } catch (e) { return 'unknown'; }
  };
  try {
    const mo = new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === 'attributes') { s.attrs++; }
        else if (m.type === 'characterData') { s.chardata++; }
        else if (m.type === 'childList') {
          m.addedNodes.forEach((x) => { s.added++; if (x.nodeType === 1) bump(s.addTags, x.tagName.toLowerCase()); bump(s.locs, loc(x)); });
          m.removedNodes.forEach((x) => { s.removed++; if (x.nodeType === 1) bump(s.remTags, x.tagName.toLowerCase()); });
        }
      }
    });
    mo.observe(document.documentElement, {childList: true, subtree: true, attributes: true, characterData: true});
  } catch (e) {}
  const shadow = () => {
    let open = 0; const hosts = [];
    try {
      const all = document.querySelectorAll('*');
      for (const el of all) {
        if (el.shadowRoot) { open++; if (hosts.length < 50) hosts.push(el.tagName.toLowerCase() + (el.id ? ('#' + el.id) : '')); }
      }
    } catch (e) {}
    return {open, hosts};
  };
  window.__naukriObs = {
    drain: () => {
      const sh = shadow();
      const out = {
        added: s.added, removed: s.removed, attrs: s.attrs, chardata: s.chardata,
        addTags: Object.assign({}, s.addTags), remTags: Object.assign({}, s.remTags),
        locs: Object.keys(s.locs).slice(0, 30),
        open_shadow_roots: sh.open, shadow_hosts: sh.hosts,
        window_ms: Date.now() - s.t0
      };
      s.added = s.removed = s.attrs = s.chardata = 0;
      s.addTags = {}; s.remTags = {}; s.locs = {}; s.t0 = Date.now();
      return out;
    }
  };
})();
"""


def _normalize_url_str(u: str) -> str:
    try:
        parts = urlsplit(u)
        return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else (parts.path or "<url>")
    except Exception:  # noqa: BLE001
        return "<url>"


def _redact_console_text(text: str) -> str:
    """First line only, URLs -> path, emails/tokens stripped, length-capped."""
    if not text:
        return ""
    line = text.splitlines()[0] if "\n" in text else text
    line = _URL_IN_TEXT_RE.sub(lambda m: _normalize_url_str(m.group(0)), line)
    line = _EMAIL_RE.sub("<email>", line)
    line = _TOKENISH_RE.sub("<redacted>", line)
    return line[:_CONSOLE_EXCERPT_MAX]


class PostInitClientObserver:
    """
    Passive, read-only observer of client-side behaviour AFTER the first
    allowed Apply-init request. Shares `MutatingRequestBlocker`'s
    monotonic `seq` and post-init gate for deterministic correlation.
    Every listener/evaluate is fully defensive — a failure is noted and
    never propagates, so it can never abort the run or stop report.json
    being written. Adds NO network permission and performs NO page
    interaction.
    """

    def __init__(self, blocker: "MutatingRequestBlocker") -> None:
        self._blocker = blocker
        self._installed = False
        self.console_events: list[ConsoleEventRecord] = []
        self.page_errors: list[ConsoleEventRecord] = []
        self.websocket_events: list[WebSocketEventRecord] = []
        self.frame_lifecycle: list[FrameLifecycleRecord] = []
        self.dom_mutation_summaries: list[DomMutationSummary] = []
        self.dom_samples: list[DomSampleRecord] = []
        self.observer_notes: list[str] = []
        self.caps: dict[str, int] = {}

    # -- infra -----------------------------------------------------------

    def _active(self) -> bool:
        return self._blocker._post_init_active()

    def _seq(self) -> int:
        return self._blocker._next_seq()

    def _note(self, msg: str) -> None:
        if len(self.observer_notes) < _CLIENT_OBS_MAX:
            self.observer_notes.append(msg)

    def _cap(self, name: str) -> bool:
        self.caps[name] = self.caps.get(name, 0) + 1
        return True

    def install(self, page: Any) -> None:
        if self._installed:
            return
        self._installed = True
        for event, handler in (
            ("console", self._on_console),
            ("pageerror", self._on_pageerror),
            ("websocket", self._on_websocket),
            ("frameattached", self._on_frameattached),
            ("framedetached", self._on_framedetached),
        ):
            try:
                page.on(event, handler)
            except Exception as exc:  # noqa: BLE001
                self._note(f"page.on({event!r}) unavailable: {type(exc).__name__}")
        add_init = getattr(page, "add_init_script", None)
        if add_init is not None:
            try:
                add_init(_MUTATION_OBSERVER_SCRIPT)
            except Exception as exc:  # noqa: BLE001
                self._note(f"add_init_script: {type(exc).__name__}")
        try:
            page.evaluate(_MUTATION_OBSERVER_SCRIPT)  # best-effort for the current document
        except Exception as exc:  # noqa: BLE001
            self._note(f"evaluate(observer script): {type(exc).__name__}")

    # -- passive listeners --------------------------------------------------

    def _on_console(self, msg: Any) -> None:
        try:
            if not self._active():
                return
            if len(self.console_events) >= _CLIENT_OBS_MAX:
                self._cap("console_events")
                return
            try:
                level = (getattr(msg, "type", None) or "").lower() or None
            except Exception:  # noqa: BLE001
                level = None
            try:
                text = getattr(msg, "text", "") or ""
            except Exception:  # noqa: BLE001
                text = ""
            url = None
            qkeys: list[str] = []
            line = None
            try:
                location = getattr(msg, "location", None) or {}
                if isinstance(location, dict):
                    raw = location.get("url")
                    if raw:
                        url, qkeys = _safe_url(raw)
                    line = location.get("lineNumber")
            except Exception:  # noqa: BLE001
                pass
            excerpt = _redact_console_text(text) if level in ("error", "warning") else None
            self.console_events.append(
                ConsoleEventRecord(
                    seq=self._seq(), kind="console", level=level,
                    message_length=len(text), message_excerpt=excerpt,
                    url=url, query_param_keys=qkeys, line=line,
                )
            )
        except Exception as exc:  # noqa: BLE001 - an observer must never break the run
            self._note(f"_on_console swallowed {type(exc).__name__}")

    def _on_pageerror(self, err: Any) -> None:
        try:
            if not self._active():
                return
            if len(self.page_errors) >= _CLIENT_OBS_MAX:
                self._cap("page_errors")
                return
            try:
                message = getattr(err, "message", None) or str(err) or ""
            except Exception:  # noqa: BLE001
                message = ""
            self.page_errors.append(
                ConsoleEventRecord(
                    seq=self._seq(), kind="pageerror", level="error",
                    message_length=len(message),
                    message_excerpt=_redact_console_text(message),
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._note(f"_on_pageerror swallowed {type(exc).__name__}")

    def _on_websocket(self, ws: Any) -> None:
        try:
            if not self._active():
                return
            if len(self.websocket_events) >= _CLIENT_OBS_MAX:
                self._cap("websocket_events")
                return
            raw = ""
            try:
                raw = getattr(ws, "url", "") or ""
            except Exception:  # noqa: BLE001
                raw = ""
            base, qkeys = _safe_url(raw)
            rec = WebSocketEventRecord(seq=self._seq(), url=base, query_param_keys=qkeys, opened=True)
            self.websocket_events.append(rec)

            def _bump(direction: str, payload: Any) -> None:
                try:
                    size = len(payload) if payload is not None else 0
                except Exception:  # noqa: BLE001
                    size = 0
                if direction == "sent":
                    rec.frames_sent += 1
                    rec.bytes_sent += size
                else:
                    rec.frames_received += 1
                    rec.bytes_received += size

            def _payload_of(frame: Any) -> Any:
                return getattr(frame, "payload", frame)

            try:
                ws.on("framesent", lambda f: _bump("sent", _payload_of(f)))
                ws.on("framereceived", lambda f: _bump("recv", _payload_of(f)))
                ws.on("close", lambda *_a: setattr(rec, "closed", True))
            except Exception as exc:  # noqa: BLE001
                rec.note = f"ws.on unavailable: {type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001
            self._note(f"_on_websocket swallowed {type(exc).__name__}")

    def _on_frameattached(self, frame: Any) -> None:
        self._record_frame_lifecycle("attached", frame)

    def _on_framedetached(self, frame: Any) -> None:
        self._record_frame_lifecycle("detached", frame)

    def _record_frame_lifecycle(self, event: str, frame: Any) -> None:
        try:
            if not self._active():
                return
            if len(self.frame_lifecycle) >= _CLIENT_OBS_MAX:
                self._cap("frame_lifecycle")
                return
            raw = ""
            try:
                raw = getattr(frame, "url", "") or ""
            except Exception:  # noqa: BLE001
                raw = ""
            base, qkeys = _safe_url(raw) if raw else ("<none>", [])
            is_main = False
            try:
                is_main = frame.parent_frame is None
            except Exception:  # noqa: BLE001
                is_main = False
            self.frame_lifecycle.append(
                FrameLifecycleRecord(
                    seq=self._seq(), event=event, url=base,
                    query_param_keys=qkeys, is_main_frame=bool(is_main),
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._note(f"_record_frame_lifecycle swallowed {type(exc).__name__}")

    # -- polled observers -------------------------------------------------

    def drain_dom_mutations(self, page: Any) -> None:
        try:
            if not self._active():
                return
            try:
                raw = page.evaluate(
                    "() => (window.__naukriObs ? window.__naukriObs.drain() : null)"
                )
            except Exception as exc:  # noqa: BLE001
                self._note(f"drain evaluate: {type(exc).__name__}")
                return
            if not isinstance(raw, dict):
                return
            if len(self.dom_mutation_summaries) >= _CLIENT_OBS_MAX:
                self._cap("dom_mutation_summaries")
                return
            self.dom_mutation_summaries.append(
                DomMutationSummary(
                    seq=self._seq(),
                    added_nodes=int(raw.get("added", 0) or 0),
                    removed_nodes=int(raw.get("removed", 0) or 0),
                    attribute_changes=int(raw.get("attrs", 0) or 0),
                    character_data_changes=int(raw.get("chardata", 0) or 0),
                    added_tag_histogram={
                        str(k): int(v) for k, v in (raw.get("addTags") or {}).items()
                    },
                    removed_tag_histogram={
                        str(k): int(v) for k, v in (raw.get("remTags") or {}).items()
                    },
                    attach_locations=[str(x) for x in (raw.get("locs") or [])][:30],
                    open_shadow_roots=int(raw.get("open_shadow_roots", 0) or 0),
                    shadow_host_summaries=[str(x) for x in (raw.get("shadow_hosts") or [])][:50],
                    window_ms=raw.get("window_ms"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._note(f"drain_dom_mutations swallowed {type(exc).__name__}")

    def sample_dom(self, page: Any, sample_index: int) -> None:
        """Re-run the EXISTING extract_application_ui() classification and
        record COUNTS only (no controls list -> no label/question text)."""
        try:
            if len(self.dom_samples) >= _CLIENT_OBS_MAX:
                self._cap("dom_samples")
                return
            try:
                ui = extract_application_ui(page)
            except Exception as exc:  # noqa: BLE001
                self.dom_samples.append(
                    DomSampleRecord(
                        seq=self._seq(), sample_index=sample_index,
                        note=f"extract_application_ui raised {type(exc).__name__}",
                    )
                )
                return
            self.dom_samples.append(
                DomSampleRecord(
                    seq=self._seq(),
                    sample_index=sample_index,
                    application_root_found=ui.application_root_found,
                    application_root_selector=ui.application_root_selector,
                    control_count=len(ui.controls),
                    question_field_count=ui.question_field_count,
                    resume_selection_control_count=ui.resume_selection_control_count,
                    final_submit_control_count=ui.final_submit_control_count,
                    frame_count=len(ui.frames),
                    dialog_count=len(ui.dialogs),
                    outer_html_chars=ui.application_root_outer_html_chars,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._note(f"sample_dom swallowed {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Post-Apply UI extraction (READ-ONLY)
# ---------------------------------------------------------------------------


def extract_application_ui(page: Any) -> ApplyUiInspection:
    """
    READ the dynamically-rendered post-Apply UI on the current page.
    Never clicks, fills, or navigates. Returns a structured description
    for Stage 2 design; absence of a field means "not observed here",
    not "confirmed absent".
    """
    # Page-level structure first — a modal / iframe / redirect target
    # can live OUTSIDE any chatbot root (Stage 1.5 spec item 17).
    frames = _collect_frames(page)
    dialogs = _collect_dialogs(page)

    root = None
    matched_selector: str | None = None
    seen_empty: list[str] = []

    for sel in selectors.APPLICATION_ROOT_CANDIDATES:
        try:
            candidate = page.query_selector(sel)
        except Exception as exc:  # noqa: BLE001
            logger.debug("query_selector(%r) raised %s; skipping", sel, exc)
            continue
        if candidate is None:
            continue
        if _has_content(candidate):
            root = candidate
            matched_selector = sel
            break
        seen_empty.append(sel)

    if root is None:
        notes = [
            "No POPULATED application root matched selectors.APPLICATION_ROOT_CANDIDATES. "
            "The post-Apply UI may use an unknown selector, may render in a popup or iframe, "
            "or the human pressed Enter before it finished rendering."
        ]
        if seen_empty:
            notes.append(f"Present but empty at capture time: {', '.join(seen_empty)}")
        if frames:
            notes.append(f"{len(frames)} frame(s)/iframe(s) present — the UI may render inside one.")
        if dialogs:
            notes.append(f"{len(dialogs)} dialog/modal-like element(s) present.")
        return ApplyUiInspection(
            application_root_found=False, frames=frames, dialogs=dialogs, notes=notes
        )

    outer_html = _safe_eval(root, "e => e.outerHTML") or ""
    try:
        visible_text = (root.inner_text() or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("inner_text() on application root raised %s", exc)
        visible_text = ""

    try:
        raw_controls = root.query_selector_all("input, select, textarea, button")
    except Exception as exc:  # noqa: BLE001
        logger.debug("query_selector_all on application root raised %s", exc)
        raw_controls = []

    controls = [_describe_control(el, root) for el in raw_controls]
    resume_count = sum(1 for c in controls if c.looks_like_resume_control)
    submit_count = sum(1 for c in controls if c.looks_like_final_submit)
    question_count = sum(1 for c in controls if c.looks_like_question)

    notes: list[str] = []
    if not controls:
        notes.append("Application root found but contained no input/select/textarea/button elements.")
    if resume_count == 0:
        notes.append("No control inside the application UI was classified as a resume selector.")
    if submit_count == 0:
        notes.append("No control inside the application UI was classified as a final submit control.")

    return ApplyUiInspection(
        application_root_found=True,
        application_root_selector=matched_selector,
        application_root_outer_html_chars=len(outer_html),
        visible_text_excerpt=(visible_text[:_VISIBLE_TEXT_EXCERPT_LIMIT] or None),
        controls=controls,
        resume_selection_control_count=resume_count,
        final_submit_control_count=submit_count,
        question_field_count=question_count,
        frames=frames,
        dialogs=dialogs,
        notes=notes,
    )


def _collect_frames(page: Any) -> list[FrameInfo]:
    try:
        els = page.query_selector_all(selectors.FRAME_SELECTOR)
    except Exception as exc:  # noqa: BLE001
        logger.debug("frame query raised %s", exc)
        return []
    out: list[FrameInfo] = []
    for el in els[:20]:
        src = _attr(el, "src")
        out.append(
            FrameInfo(
                src=(_safe_url(src)[0] if src else None),
                element_id=_attr(el, "id"),
                name=_attr(el, "name"),
                title=_attr(el, "title"),
            )
        )
    return out


def _collect_dialogs(page: Any) -> list[DialogInfo]:
    out: list[DialogInfo] = []
    seen: set[tuple[str | None, str | None]] = set()
    for sel in selectors.DIALOG_SELECTOR_CANDIDATES:
        try:
            els = page.query_selector_all(sel)
        except Exception as exc:  # noqa: BLE001
            logger.debug("dialog query_selector_all(%r) raised %s", sel, exc)
            continue
        for el in els:
            tag = (_safe_eval(el, "e => e.tagName && e.tagName.toLowerCase()") or "").lower() or "unknown"
            cid = _attr(el, "id")
            key = (tag, cid)
            if key in seen:
                continue
            seen.add(key)
            try:
                text = (el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
            out.append(
                DialogInfo(
                    tag=tag,
                    element_id=cid,
                    role=_attr(el, "role"),
                    aria_modal=_attr(el, "aria-modal"),
                    text_excerpt=(text[:400] or None),
                )
            )
            if len(out) >= 20:
                return out
    return out


def _has_content(el: Any) -> bool:
    try:
        if (el.inner_text() or "").strip():
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(el.query_selector_all("*"))
    except Exception:  # noqa: BLE001
        return False


def _safe_eval(el: Any, script: str) -> Any:
    try:
        return el.evaluate(script)
    except Exception as exc:  # noqa: BLE001
        logger.debug("evaluate(%r) raised %s", script, exc)
        return None


def _attr(el: Any, name: str) -> str | None:
    try:
        v = el.get_attribute(name)
    except Exception:  # noqa: BLE001
        return None
    return v.strip() if isinstance(v, str) else v


def _describe_control(el: Any, root: Any) -> ApplyControl:
    tag = (_safe_eval(el, "e => e.tagName && e.tagName.toLowerCase()") or "").lower() or None
    ctype = _attr(el, "type")
    cid = _attr(el, "id")
    name = _attr(el, "name")
    placeholder = _attr(el, "placeholder")
    aria_label = _attr(el, "aria-label")
    try:
        text = (el.inner_text() or "").strip() or None
    except Exception:  # noqa: BLE001
        text = None
    label_text = _label_for(root, cid)
    disabled = _attr(el, "disabled") is not None or _safe_eval(el, "e => !!e.disabled") is True

    classify_blob = " ".join(
        p.lower() for p in (ctype, name, cid, placeholder, aria_label, text, label_text) if p
    )
    looks_resume = any(h in classify_blob for h in _RESUME_CONTROL_HINTS)

    submit_blob = " ".join(p.lower() for p in (text, aria_label, cid, _attr(el, "value")) if p)
    is_buttonish = (tag == "button") or (ctype in {"submit", "button", "image"})
    looks_submit = is_buttonish and any(h in submit_blob for h in _FINAL_SUBMIT_HINTS)

    # A field that would hold a user's answer: a textarea, a select, or
    # an <input> that isn't a button/submit/reset/file/hidden — and
    # that isn't already classified as a resume or final-submit control.
    _NON_ANSWER_INPUT_TYPES = {"submit", "button", "image", "reset", "hidden", "file"}
    is_answerable = tag in {"textarea", "select"} or (
        tag == "input" and (ctype or "text").lower() not in _NON_ANSWER_INPUT_TYPES
    )
    looks_question = bool(is_answerable and not looks_resume and not looks_submit)

    return ApplyControl(
        tag=tag or "unknown",
        type=ctype,
        element_id=cid,
        name=name,
        placeholder=placeholder,
        aria_label=aria_label,
        text=text,
        label_text=label_text,
        disabled=bool(disabled),
        looks_like_resume_control=looks_resume,
        looks_like_final_submit=looks_submit,
        looks_like_question=looks_question,
    )


def _label_for(root: Any, control_id: str | None) -> str | None:
    if not control_id:
        return None
    try:
        lbl = root.query_selector(f"label[for='{control_id}']")
    except Exception:  # noqa: BLE001
        return None
    if lbl is None:
        return None
    try:
        return (lbl.inner_text() or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_apply_inspection(
    settings: Settings,
    job_url: str,
    *,
    isolated_profile: bool = True,
    allow_apply_init: bool = True,
    wait_for_manual_apply: Callable[[str], None] | None = None,
    wait_for_manual_completion: Callable[[str], None] | None = None,
) -> dict:
    """
    Run the Stage 1.5 controlled post-Apply inspection end to end.

    isolated_profile=True (default) launches the browser with a fresh,
    disposable user-data dir under inspection_output/, so this never
    touches the normal persistent session.

    allow_apply_init=True (default) narrowly allowlists the ONE observed
    apply-initialisation POST (_APPLY_INIT_PATH) so the post-Apply UI
    can actually render; every other mutating request — including any
    submit/confirm endpoint — stays default-denied. Set False for the
    pure default-deny baseline.

    wait_for_manual_apply / wait_for_manual_completion are injectable
    blocking callables (tests supply their own); both default to input().
    wait_for_manual_apply is the "you click Apply now" pause;
    wait_for_manual_completion is Stage 1's unchanged CAPTCHA/MFA pause.
    """
    if wait_for_manual_apply is None:
        wait_for_manual_apply = input
    if wait_for_manual_completion is None:
        wait_for_manual_completion = input

    from playwright.sync_api import Error as PlaywrightError

    now = datetime.datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    out_dir = settings.inspection_output_dir / f"apply_{stamp}"
    profile_dir = (
        settings.inspection_output_dir / "_apply_session_profiles" / stamp
        if isolated_profile
        else None
    )

    report: dict = {
        "kind": "apply_inspection_stage_1_5",
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "job_url": job_url,
        "isolated_profile": bool(isolated_profile),
        "profile_dir": str(profile_dir) if profile_dir else None,
        "steps": [],
        "network_blocker": {
            "policy": (
                "default-deny: every request whose method is not GET/HEAD/OPTIONS is blocked "
                "once armed; armed immediately after login; never disarmed while the browser "
                "is open; request headers are never read"
            ),
            "apply_init_allowlist": (
                sorted(f"{m} {p}" for m, p in _APPLY_INIT_ALLOWLIST) if allow_apply_init else []
            ),
            "apply_init_allowlist_match": "exact (method, url-path) equality — no substring/prefix/regex",
            "armed_before_manual_apply": False,
            "disarmed": False,
        },
        "completed": False,
    }

    blocker = MutatingRequestBlocker(
        allow_exact=_APPLY_INIT_ALLOWLIST if allow_apply_init else frozenset()
    )
    client_obs = PostInitClientObserver(blocker)

    def _persist_report() -> None:
        """
        Write report.json with whatever the run has so far. Safe to call
        repeatedly (the last call wins). Deliberately OUTSIDE every
        failure path: an EOFError / KeyboardInterrupt / unexpected raise
        after '01_pre_apply' must never again leave a run with no report.
        """
        _snapshot_blocker_state(report, blocker)
        _snapshot_client_observations(report, client_obs)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "report.json").write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 - reporting must not raise
            logger.error("Could not write report.json: %s", exc)

    try:
        with BrowserManager(settings, profile_dir_override=profile_dir) as browser:
            client = NaukriClient(browser.page, settings)

            route_target = getattr(browser, "context", None) or browser.page
            blocker.install(route_target)
            # Frame navigations are page-level events (a BrowserContext
            # does not emit them) — needed for the post-init transition
            # inspection (does anything navigate to /myapply/saveApply?).
            blocker.observe_navigation(browser.page)
            # Passive client-workflow observers (console / pageerror /
            # websocket / frame lifecycle + an aggregate MutationObserver).
            # Read-only; adds no network permission; never interacts.
            client_obs.install(browser.page)

            try:
                try:
                    login_result = client.login()
                except (NaukriCaptchaError, NaukriMfaError) as exc:
                    login_result = _handle_login_challenge(
                        client, browser.page, out_dir, exc, wait_for_manual_completion
                    )
                report["steps"].append({"step": "login", "status": login_result.status.value})

                # From here on NOTHING mutating is allowed to leave the browser.
                blocker.arm()
                report["network_blocker"]["armed_before_manual_apply"] = True

                # Bound every subsequent auto-wait. On Naukri 'networkidle'
                # is often never reached (busy SPA), and the armed guard
                # aborts the chatbot's own XHRs so it keeps retrying — an
                # unbounded default would turn each wait into a 30s stall.
                try:
                    browser.page.set_default_timeout(15000)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("set_default_timeout raised %s; ignoring", exc)

                # Navigate to the test job — read-only, never clicks Apply.
                workflow = client.get_job(job_url)
                report["steps"].append(
                    {"step": "open_job_listing", "job_url": job_url, "result": workflow.model_dump()}
                )
                _save_snapshot(browser.page, out_dir, "01_pre_apply")

                # Partial report NOW — so a hang or kill during the human
                # pause still leaves a report.json on disk.
                report["steps"].append({"step": "awaiting_manual_apply"})
                _persist_report()

                # Hand control to the human. The automation clicks nothing.
                blocked_before_wait = len(blocker.blocked)
                try:
                    wait_for_manual_apply(_MANUAL_APPLY_PROMPT)
                except EOFError as exc:
                    raise NaukriAutomationError(
                        "The manual-apply pause could not read from stdin (EOFError). "
                        "`naukri-agent inspect-apply` must be run in an interactive terminal "
                        "— not through a non-interactive runner, a pipe, or a background "
                        "process. Re-run it directly in a shell where you can press Enter."
                    ) from exc
                report["steps"].append({"step": "manual_apply_confirmed"})

                blocked_during_wait = len(blocker.blocked) - blocked_before_wait
                if blocked_during_wait:
                    report.setdefault("notes", []).append(
                        f"{blocked_during_wait} mutating request(s) were blocked during the "
                        "manual-apply window by the default-deny guard (as designed). The "
                        "post-Apply UI may therefore be only partially rendered — this is "
                        "expected and the guard is NOT weakened."
                    )

                # Best-effort settle — bounded, never fatal.
                try:
                    browser.page.wait_for_load_state("networkidle", timeout=5000)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("post-apply settle wait raised %s; continuing", exc)

                # Bounded, repeated post-init client-workflow sampling.
                # The in-page MutationObserver has been buffering for the
                # WHOLE post-Apply window (incl. the human pause); each
                # Playwright call below also pumps buffered console /
                # pageerror / websocket / frame events. Nothing is
                # clicked / filled / focused. Bounded:
                # _DOM_SAMPLE_COUNT * _DOM_SAMPLE_INTERVAL_MS.
                report["steps"].append({"step": "post_init_client_sampling"})
                client_obs.drain_dom_mutations(browser.page)
                client_obs.sample_dom(browser.page, 0)
                for _sample_i in range(1, _DOM_SAMPLE_COUNT):
                    try:
                        browser.page.wait_for_timeout(_DOM_SAMPLE_INTERVAL_MS)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("sample interval wait raised %s; continuing", exc)
                    client_obs.drain_dom_mutations(browser.page)
                    client_obs.sample_dom(browser.page, _sample_i)
                _persist_report()

                # Extraction must never be fatal to the report.
                extraction_error: str | None = None
                try:
                    apply_ui = client.extract_application_ui()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("extract_application_ui failed")
                    extraction_error = f"{type(exc).__name__}: {exc}"
                    apply_ui = ApplyUiInspection(
                        application_root_found=False,
                        notes=[f"extract_application_ui raised {extraction_error}"],
                    )
                report["steps"].append(
                    {"step": "extract_application_ui", "result": apply_ui.model_dump()}
                )

                _save_snapshot(browser.page, out_dir, "02_post_apply")
                out_dir.mkdir(parents=True, exist_ok=True)
                _write_application_root_html(
                    browser.page, apply_ui, out_dir / "application_root.html"
                )

                report["apply_ui"] = apply_ui.model_dump()
                if extraction_error:
                    report["error_type"] = "ExtractionError"
                    report["error"] = extraction_error
                    report["completed"] = False
                else:
                    report["completed"] = True

            except NaukriAutomationError as exc:
                _record_failure(report, browser.page, out_dir, exc)
            except PlaywrightError as exc:
                _record_failure(report, browser.page, out_dir, exc)
            except Exception as exc:  # noqa: BLE001 - incl. EOFError; never lose the report
                _record_failure(report, browser.page, out_dir, exc)
    except BaseException as exc:  # noqa: BLE001 - KeyboardInterrupt/SystemExit: record, persist, re-raise
        report.setdefault("error_type", type(exc).__name__)
        report.setdefault("error", str(exc))
        report.setdefault(
            "stopped_at",
            report["steps"][-1]["step"] if report["steps"] else "before_browser_launch",
        )
        report["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
        _persist_report()
        raise
    finally:
        # GUARANTEED: whatever happened above, a report.json is on disk.
        report.setdefault("finished_at", datetime.datetime.now(datetime.UTC).isoformat())
        _persist_report()

    report["output_dir"] = str(out_dir)
    return report


def _snapshot_blocker_state(report: dict, blocker: "MutatingRequestBlocker") -> None:
    """Refresh report['network_blocker'] from the live guard. Pure
    Python — touches no browser state — so it is safe to call from
    _persist_report() at any point, including after a failure."""
    nb = report.get("network_blocker")
    if nb is None:
        return
    nb["armed_final"] = blocker.armed
    nb["disarmed"] = False
    nb["blocked"] = [r.model_dump() for r in blocker.blocked]
    nb["blocked_count"] = len(blocker.blocked)
    nb["observed_total"] = blocker.observed_total
    nb["observed_sample"] = [r.model_dump() for r in blocker.observed_sample]
    nb["login_phase_passthrough"] = [r.model_dump() for r in blocker.login_phase_passthrough]
    nb["allowed_apply_init_requests"] = [
        r.model_dump() for r in blocker.allowed_apply_init_requests
    ]
    nb["apply_init_request_count"] = len(blocker.allowed_apply_init_requests)
    nb["apply_init_responses"] = [r.model_dump() for r in blocker.apply_init_responses]
    nb["apply_init_response_count"] = len(blocker.apply_init_responses)
    # kept for backward compatibility with earlier reports
    nb["apply_init_allowed_count"] = len(blocker.apply_init_responses)
    nb["apply_init_records_capped"] = blocker.apply_init_records_capped
    # -- post-init transition inspection (read-only) --
    nb["apply_init_marker_seq"] = blocker.apply_init_marker_seq
    nb["post_init_events"] = [r.model_dump() for r in blocker.post_init_events]
    nb["post_init_event_count"] = len(blocker.post_init_events)
    nb["post_init_events_capped"] = blocker.post_init_events_capped
    nb["save_apply_events"] = [r.model_dump() for r in blocker.save_apply_events]
    nb["save_apply_seen"] = len(blocker.save_apply_events) > 0
    nb["save_apply_watch_path"] = _SAVE_APPLY_PATH  # observed, never allowlisted


def _snapshot_client_observations(report: dict, obs: "PostInitClientObserver") -> None:
    """Refresh report['post_init_client_observations'] from the passive
    client-workflow observer. Pure Python — no browser access — so it is
    safe from _persist_report() at any point."""
    report["post_init_client_observations"] = {
        "purpose": (
            "read-only: why the questionnaire returned by Apply-init does not visibly "
            "render. Metadata only — no body/header/cookie/token/PII/DOM-text/answers."
        ),
        "console_events": [r.model_dump() for r in obs.console_events],
        "page_errors": [r.model_dump() for r in obs.page_errors],
        "websocket_events": [r.model_dump() for r in obs.websocket_events],
        "frame_lifecycle": [r.model_dump() for r in obs.frame_lifecycle],
        "dom_mutation_summaries": [r.model_dump() for r in obs.dom_mutation_summaries],
        "dom_samples": [r.model_dump() for r in obs.dom_samples],
        "observer_notes": list(obs.observer_notes),
        "counts": {
            "console_events": len(obs.console_events),
            "page_errors": len(obs.page_errors),
            "websocket_events": len(obs.websocket_events),
            "frame_lifecycle": len(obs.frame_lifecycle),
            "dom_mutation_summaries": len(obs.dom_mutation_summaries),
            "dom_samples": len(obs.dom_samples),
        },
        "caps_hit": dict(obs.caps),
        "max_per_collection": _CLIENT_OBS_MAX,
        "dom_sample_plan": {
            "count": _DOM_SAMPLE_COUNT,
            "interval_ms": _DOM_SAMPLE_INTERVAL_MS,
        },
    }


def _write_application_root_html(page: Any, apply_ui: ApplyUiInspection, path: Any) -> None:
    if not apply_ui.application_root_found:
        path.write_text(
            "<!-- Stage 1.5: no populated application root was detected. See report.json notes. -->",
            encoding="utf-8",
        )
        return
    html = ""
    for sel in selectors.APPLICATION_ROOT_CANDIDATES:
        try:
            el = page.query_selector(sel)
        except Exception:  # noqa: BLE001
            el = None
        if el is not None:
            html = _safe_eval(el, "e => e.outerHTML") or ""
            if html:
                break
    path.write_text(html or "<!-- outerHTML unavailable -->", encoding="utf-8")
