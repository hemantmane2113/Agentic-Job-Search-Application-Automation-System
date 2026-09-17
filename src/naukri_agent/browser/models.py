"""
Structured result types returned by browser/login.py, profile.py, and
jobs.py. Nothing outside browser/ should need to know Playwright's
types — these are what the rest of the system (NaukriClient's
callers) actually sees.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class LoginStatus(str, enum.Enum):
    SUCCESS = "success"


class LoginResult(BaseModel):
    status: LoginStatus
    message: str
    current_url: str | None = None


class ResumeState(BaseModel):
    """
    Best-effort structured snapshot of the profile's resume section.
    Naukri's exact DOM has not yet been verified (Stage 1 — see
    browser/selectors.py) so treat any None/False field as
    "not yet determined", not "confirmed absent".
    """

    resume_filename: str | None = None
    last_updated_text: str | None = None
    upload_control_present: bool = False
    remove_control_present: bool = False


class JobListingSummary(BaseModel):
    """One row from a Naukri search-results page — inspection-only, not the full JobCreate shape."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None
    # Raw relative posted-date label off the search card (e.g. "Just now",
    # "Few hours ago", "Today", "1 Day Ago", "30+ Days Ago"). Parsed
    # deterministically by the discovery freshness gate; never sent to an LLM.
    posted_text: str | None = None


class KeySkillChip(BaseModel):
    """
    One chip from Naukri's visible "Key Skills" widget (distinct from
    the JD body captured in JobDetail.description — see
    browser/selectors.py's KEY_SKILLS_* comment for the real capture
    this was evidenced from). `preferred` reflects presence of the
    widget's own documented icon (its on-page legend reads "Skills
    highlighted with [icon] are preferred keyskills") — this is
    Naukri's own tag, read verbatim, never an inference we make.
    """

    text: str
    preferred: bool = False


class JobDetail(BaseModel):
    """
    Read-only extract of a job's public listing DETAIL page — the input
    the daily digest needs. Every field is degrade-to-None: a missing
    selector never raises. `url` is always the URL we navigated to.
    """

    url: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    salary_text: str | None = None
    experience_text: str | None = None
    description: str | None = None
    posted_date_text: str | None = None
    source: str = "naukri"
    # Raw skill evidence — scraped, never LLM-derived, and not yet
    # persisted or consumed by scoring in this phase (see
    # jobs/skill_evidence.py for the derived-layer merge these will
    # eventually feed). Each degrades to None independently, same
    # convention as every field above.
    ld_json_skills: list[str] | None = None
    key_skills_dom: list[KeySkillChip] | None = None


class ApplicationWorkflowInspection(BaseModel):
    """
    What was OBSERVED on a job's apply workflow — never an executed
    application. This is Stage 1's output for designing Stage 2, not
    something Stage 2 itself produces.
    """

    apply_button_present: bool = False
    resume_selection_controls_present: bool = False
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 1.5 apply-inspection result types (browser/apply_inspection.py)
#
# Stage 1.5 is a CONTROLLED, human-driven observation of the post-Apply
# UI. It performs no write operation: the human clicks Apply, a
# default-deny network guard blocks every mutating request, and the
# tool only READS the resulting UI. These models carry deliberately
# secret-free data — request PATHS and param/body KEY NAMES only, never
# values, cookies, tokens, or headers.
# ---------------------------------------------------------------------------


class BlockedRequestRecord(BaseModel):
    """
    One mutating request the guard refused to let leave the browser.
    Everything here is safe to persist: no header is ever read, and
    only key *names* (never values) are recorded from the URL query and
    the request body.
    """

    method: str
    url: str  # scheme://host/path only — query string deliberately dropped
    query_param_keys: list[str] = Field(default_factory=list)  # names only, never values
    resource_type: str | None = None
    is_navigation: bool = False
    post_data_present: bool = False
    post_data_size_bytes: int = 0
    post_data_top_level_keys: list[str] = Field(default_factory=list)  # names only, never values
    reason: str = "default-deny: non-GET/HEAD/OPTIONS method blocked during apply inspection"


class ObservedRequestRecord(BaseModel):
    """A request the guard allowed (a GET/HEAD/OPTIONS, or a login-phase
    mutating request that passed through before the guard was armed).
    Method + path only."""

    method: str
    url: str
    resource_type: str | None = None


class AppInitResponseRecord(BaseModel):
    """
    Metadata + SANITIZED SHAPE for the ONE narrowly-allowlisted
    apply-initialisation POST (see apply_inspection._APPLY_INIT_ALLOWLIST).

    Never stored: the raw response body, headers, cookies, tokens, PII,
    resume/profile data, or arbitrary string contents. `url` is
    scheme://host/path with the query string dropped. `json_shape` is a
    recursive description — object key NAMES, array LENGTHS, primitive
    TYPES, string LENGTHS, and for URL-looking strings only
    scheme/host/path — built with depth/width/node budgets so it cannot
    grow unbounded. The only literal values it may contain are booleans
    and, for an explicitly allowlisted set of enum-like keys
    (apply_inspection._SHAPE_SAFE_ENUM_KEYS), a short safe-charset
    string / finite number.
    """

    request_index: int = 1  # 1-based order among allowed apply-init requests this run
    seq: int = 0  # global observation order (correlates with post_init_events)
    method: str
    url: str
    status: int | None = None
    content_type: str | None = None  # media type only, params stripped
    body_size_bytes: int | None = None  # None = not safely available (e.g. page navigated)
    json_top_level_keys: list[str] = Field(default_factory=list)  # key NAMES only
    json_shape: dict | None = None  # sanitized recursive shape; no body, no values
    json_shape_truncated: bool = False
    note: str | None = None


class ApplyInitRequestRecord(BaseModel):
    """
    REQUEST-side record for one allowlisted apply-init POST (recorded in
    `_handle` when the request is let through). Paired with an
    AppInitResponseRecord captured from the `response` event. Comparing
    the two counts is the signal for "genuine SPA retries" vs "lost
    responses / double observation". All fields are already
    secret-free (key NAMES and sizes only).
    """

    request_index: int = 1
    seq: int = 0  # global observation order (correlates with post_init_events)
    method: str
    url: str  # scheme://host/path — query dropped
    resource_type: str | None = None
    is_navigation: bool = False
    post_data_size_bytes: int = 0
    post_data_top_level_keys: list[str] = Field(default_factory=list)  # NAMES only


class PostInitEventRecord(BaseModel):
    """
    A GET request, a response, a frame navigation, or a (still-blocked)
    mutation OBSERVED after the first allowed Apply-init request — the
    "post-init transition inspection". This never allows or blocks
    anything; it is pure read-only observation. Sanitized metadata
    only: never a body, header, cookie, token, or raw query value.
    """

    seq: int
    kind: str  # "get-request" | "response" | "navigation" | "blocked-mutation"
    method: str | None = None
    url: str  # scheme://host/path — query dropped
    query_param_keys: list[str] = Field(default_factory=list)  # NAMES only, never values
    resource_type: str | None = None
    is_navigation: bool = False
    status: int | None = None
    content_type: str | None = None  # media type only
    is_save_apply: bool = False  # path == /myapply/saveApply (from applyRedirectUrl; observed, not guessed)
    after_apply_init: bool = True
    note: str | None = None


# ---------------------------------------------------------------------------
# Post-init client-workflow observation (apply_inspection.PostInitClientObserver)
#
# Passive, read-only observers of client-side behaviour AFTER the allowed
# Apply-init response, so we can tell why the questionnaire the server
# returns does not visibly render. Metadata-first. NEVER persisted: any
# body/header/cookie/token/credential value, questionnaire text or
# answers, PII, raw query values, DOM text, attribute values, WS frame
# payloads, arbitrary console output.
# ---------------------------------------------------------------------------


class ConsoleEventRecord(BaseModel):
    """One `console` message or `pageerror`. `message_excerpt` is a
    redacted, length-capped first line, kept only for error/warning
    levels; everything else is counts + level + normalized location."""

    seq: int
    kind: str  # "console" | "pageerror"
    level: str | None = None  # "error"/"warning"/"log"/... ; None if unknown
    message_length: int = 0
    message_excerpt: str | None = None  # redacted + capped; errors/warnings only
    url: str | None = None  # normalized scheme://host/path of the source location
    query_param_keys: list[str] = Field(default_factory=list)  # NAMES only
    line: int | None = None
    note: str | None = None


class WebSocketEventRecord(BaseModel):
    """A WebSocket opened after Apply-init. Frame COUNTS and BYTE SIZES
    only — frame payloads are never read or stored."""

    seq: int
    url: str  # scheme://host/path — query dropped
    query_param_keys: list[str] = Field(default_factory=list)
    opened: bool = True
    closed: bool = False
    frames_sent: int = 0
    frames_received: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    note: str | None = None


class FrameLifecycleRecord(BaseModel):
    """A frame/iframe attach or detach after Apply-init. Path only."""

    seq: int
    event: str  # "attached" | "detached"
    url: str  # scheme://host/path — query dropped
    query_param_keys: list[str] = Field(default_factory=list)
    is_main_frame: bool = False
    note: str | None = None


class DomMutationSummary(BaseModel):
    """AGGREGATE MutationObserver drain — counts, tag histograms, safe
    attach-location selectors, and open-shadow-root counts. Never any
    DOM text, attribute value, input value, or HTML."""

    seq: int
    added_nodes: int = 0
    removed_nodes: int = 0
    attribute_changes: int = 0
    character_data_changes: int = 0
    added_tag_histogram: dict[str, int] = Field(default_factory=dict)
    removed_tag_histogram: dict[str, int] = Field(default_factory=dict)
    attach_locations: list[str] = Field(default_factory=list)  # e.g. "body", "div#chatbot-container"
    open_shadow_roots: int = 0
    shadow_host_summaries: list[str] = Field(default_factory=list)  # "tag#id" only
    window_ms: int | None = None
    note: str | None = None


class DomSampleRecord(BaseModel):
    """One timed re-run of the EXISTING extract_application_ui()
    classification — COUNTS only, so we can see whether/when the generic
    classifier ever detects a rendered workflow."""

    seq: int
    sample_index: int
    application_root_found: bool = False
    application_root_selector: str | None = None  # one of our fixed candidates, not PII
    control_count: int = 0
    question_field_count: int = 0
    resume_selection_control_count: int = 0
    final_submit_control_count: int = 0
    frame_count: int = 0
    dialog_count: int = 0
    outer_html_chars: int = 0
    note: str | None = None


class FrameInfo(BaseModel):
    """A <frame>/<iframe> found in the post-Apply page. `src` is a
    path only (query dropped)."""

    src: str | None = None
    element_id: str | None = None
    name: str | None = None
    title: str | None = None


class DialogInfo(BaseModel):
    """A dialog/modal-like element found in the post-Apply page."""

    tag: str
    element_id: str | None = None
    role: str | None = None
    aria_modal: str | None = None
    text_excerpt: str | None = None


class ApplyControl(BaseModel):
    """
    One input/select/textarea/button found INSIDE the dynamically
    rendered application UI. Recorded for design analysis only — the
    inspection code never operates any of these.
    """

    tag: str
    type: str | None = None
    element_id: str | None = None
    name: str | None = None
    placeholder: str | None = None
    aria_label: str | None = None
    text: str | None = None
    label_text: str | None = None
    disabled: bool = False
    looks_like_resume_control: bool = False
    looks_like_final_submit: bool = False
    looks_like_question: bool = False


class ApplyUiInspection(BaseModel):
    """
    Structured read of the post-Apply UI. Absence of a field
    (root not found, zero controls) means "not observed in this
    capture", never "confirmed not to exist" — same caveat as
    ResumeState.
    """

    application_root_found: bool = False
    application_root_selector: str | None = None
    application_root_outer_html_chars: int = 0
    visible_text_excerpt: str | None = None
    controls: list[ApplyControl] = Field(default_factory=list)
    resume_selection_control_count: int = 0
    final_submit_control_count: int = 0
    question_field_count: int = 0
    frames: list[FrameInfo] = Field(default_factory=list)
    dialogs: list[DialogInfo] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
