"""
Tests for browser.apply_inspection — Stage 1.5 CONTROLLED post-Apply
inspection. All run against fakes; no real browser or network.

They prove the safety contract the capability was built to guarantee:

  * automation never clicks Apply or any application control
  * the manual "you click Apply now" wait happens while the browser is
    still open
  * POST / PUT / PATCH / DELETE (and unknown methods) are blocked once
    the guard is armed — default-deny, no endpoint guessing
  * GET / HEAD / OPTIONS are observed, not blocked
  * blocked-request logs carry no secrets (no header access, no query
    or body values)
  * the guard is armed before the human is prompted, and is never
    disarmed / unrouted for the life of the browser
  * post-Apply DOM extraction works against a fake populated container
  * the extraction code clicks nothing
  * CAPTCHA/MFA handling is unchanged (browser stays open, then resumes)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from naukri_agent.browser import apply_inspection as apply_module
from naukri_agent.browser import selectors
from naukri_agent.browser.apply_inspection import (
    _APPLY_INIT_ALLOWLIST,
    _APPLY_INIT_PATH,
    _CLIENT_OBS_MAX,
    _DOM_SAMPLE_COUNT,
    _DOM_SAMPLE_INTERVAL_MS,
    _json_shape_root,
    _redact_console_text,
    _url_shape,
    MutatingRequestBlocker,
    PostInitClientObserver,
    extract_application_ui,
    run_apply_inspection,
)
from naukri_agent.browser.models import (
    ConsoleEventRecord,
    DomMutationSummary,
    DomSampleRecord,
    FrameLifecycleRecord,
    WebSocketEventRecord,
)
from naukri_agent.config import Settings

from .browser_fakes import FakeElement, FakePage

TEST_JOB_URL = (
    "https://www.naukri.com/job-listings-gen-ai-data-scientist-sigma-allied-services-"
    "pune-gurugram-bengaluru-2-to-7-years-040926008523"
)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


class FakeBrowserManager:
    """Stands in for browser.browser_manager.BrowserManager."""

    def __init__(self, settings, profile_dir_override=None) -> None:
        self.settings = settings
        self.profile_dir_override = profile_dir_override
        self.page = FakePage()
        self.context = None  # -> route target falls back to .page
        self.closed = False

    def __enter__(self) -> "FakeBrowserManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.closed = True


def _settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        naukri_email="a@b.com",
        naukri_password="pw",
        inspection_output_dir=tmp_path / "inspection_output",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _patch_bm(monkeypatch, manager: FakeBrowserManager) -> None:
    monkeypatch.setattr(
        apply_module,
        "BrowserManager",
        lambda settings, profile_dir_override=None: manager,
    )


def _make_authenticated(page: FakePage) -> None:
    """Persistent-session shortcut: login() returns success without ever
    touching the form (no goto/fill/click)."""
    page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())


def _populated_application_root() -> FakeElement:
    radio = FakeElement(
        attrs={"type": "radio", "id": "res1", "name": "resumeChoice"},
        tag="input",
    )
    file_input = FakeElement(
        attrs={"type": "file", "id": "cvUpload", "aria-label": "Upload a new resume"},
        tag="input",
    )
    textarea = FakeElement(
        attrs={"id": "expYears", "aria-label": "Total experience in years"},
        tag="textarea",
    )
    submit_btn = FakeElement(text="Submit", attrs={}, tag="button")
    return FakeElement(
        text="Upload your resume and answer a few questions before you submit",
        children={
            "input, select, textarea, button": [radio, file_input, textarea, submit_btn],
            "label[for='res1']": FakeElement(text="Hemant_Mane.pdf"),
        },
        outer_html="<div id='chatbot-container'><form>...populated...</form></div>",
    )


# --------------------------------------------------------------------------
# 1. Automation never clicks Apply or any control
# --------------------------------------------------------------------------


def test_automation_never_clicks_apply_or_any_control(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    manager.page.set_element(selectors.APPLY_BUTTON, FakeElement())
    _patch_bm(monkeypatch, manager)

    run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=lambda p: None
    )

    # Nothing at all was clicked by the automation — not Apply, not the
    # submit button inside the populated container.
    assert manager.page.clicked == []
    assert manager.page.filled == {}


# --------------------------------------------------------------------------
# 2. Manual wait happens while the browser is still open
# --------------------------------------------------------------------------


def test_manual_apply_wait_happens_while_browser_open(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    seen = {}

    def fake_wait(prompt: str) -> None:
        seen["closed_during_wait"] = manager.closed
        seen["prompt"] = prompt

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    assert seen["closed_during_wait"] is False
    assert "Click Apply yourself" in seen["prompt"]
    assert "do not answer questions" in seen["prompt"].lower()
    assert report["completed"] is True
    assert manager.closed is True  # closed normally at the end


# --------------------------------------------------------------------------
# 3. Mutating requests are blocked once armed (default-deny)
# --------------------------------------------------------------------------


def test_mutating_requests_blocked_under_default_deny(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    routes = {}

    def fake_wait(prompt: str) -> None:
        # Guard is armed by now — simulate an accidental application POST
        # plus the other mutating verbs and an unrecognised one.
        for method in ("POST", "PUT", "PATCH", "DELETE", "TRACE", "FOOBAR"):
            routes[method] = manager.page.simulate_request(
                method, "https://www.naukri.com/cloudgateway-mynaukri/apply"
            )

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    for method, route in routes.items():
        assert route.action == "abort", f"{method} was not blocked"
        assert route.abort_error_code == "blockedbyclient"

    blocked_methods = {b["method"] for b in report["network_blocker"]["blocked"]}
    assert blocked_methods == {"POST", "PUT", "PATCH", "DELETE", "TRACE", "FOOBAR"}
    assert report["network_blocker"]["blocked_count"] == 6


# --------------------------------------------------------------------------
# 4. Safe methods are observed, not blocked
# --------------------------------------------------------------------------


def test_get_head_options_are_observed_not_blocked(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    routes = {}

    def fake_wait(prompt: str) -> None:
        for method in ("GET", "HEAD", "OPTIONS"):
            routes[method] = manager.page.simulate_request(
                method, "https://www.naukri.com/static/app.js", resource_type="script"
            )

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    for method, route in routes.items():
        assert route.action == "continue", f"{method} should not be blocked"

    assert report["network_blocker"]["blocked"] == []
    observed_methods = {o["method"] for o in report["network_blocker"]["observed_sample"]}
    assert {"GET", "HEAD", "OPTIONS"}.issubset(observed_methods)
    assert report["network_blocker"]["observed_total"] >= 3


# --------------------------------------------------------------------------
# 5. Blocked-request logs contain no secrets
# --------------------------------------------------------------------------


def test_blocked_request_log_has_no_secrets(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    secret_url = (
        "https://www.naukri.com/cloudgateway-mynaukri/apply"
        "?ssoToken=SSO_SECRET_VALUE&jobId=12345"
    )
    secret_body = json.dumps(
        {"resumeId": "r1", "otp": "998877", "authToken": "AUTH_SECRET_VALUE"}
    )

    def fake_wait(prompt: str) -> None:
        manager.page.simulate_request("POST", secret_url, post_data=secret_body)

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    blocked = report["network_blocker"]["blocked"]
    assert len(blocked) == 1
    record = blocked[0]

    serialized = json.dumps(report)
    for secret in ("SSO_SECRET_VALUE", "AUTH_SECRET_VALUE", "998877"):
        assert secret not in serialized

    # URL is stored path-only; param + body KEY NAMES are kept (useful,
    # not sensitive), values are not.
    assert record["url"] == "https://www.naukri.com/cloudgateway-mynaukri/apply"
    assert record["query_param_keys"] == ["ssoToken", "jobId"]
    assert record["post_data_top_level_keys"] == ["authToken", "otp", "resumeId"]
    assert record["post_data_present"] is True
    assert record["post_data_size_bytes"] == len(secret_body.encode("utf-8"))

    # The guard never read request headers (cookies / authorization).
    assert all(r.headers_access_count == 0 for r in manager.page.requests)


# --------------------------------------------------------------------------
# 6. Guard armed before the manual prompt; never disarmed / unrouted
# --------------------------------------------------------------------------


def test_guard_armed_before_prompt_and_never_disarmed(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    armed_state = {}

    def fake_wait(prompt: str) -> None:
        # Prove "armed" by side effect: a POST right now must be blocked.
        route = manager.page.simulate_request("POST", "https://www.naukri.com/apply")
        armed_state["post_blocked_at_prompt"] = route.action == "abort"

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    assert armed_state["post_blocked_at_prompt"] is True
    assert report["network_blocker"]["armed_before_manual_apply"] is True
    assert report["network_blocker"]["armed_final"] is True
    assert report["network_blocker"]["disarmed"] is False
    # The guard is never torn down: exactly one route installed, no unroute.
    assert len(manager.page.routes) == 1
    assert manager.page.unroute_calls == []


def test_guard_not_armed_during_login_phase_then_armed_after(tmp_path, monkeypatch):
    """A mutating request during the (automated) login phase passes
    through but is recorded; the same request after arming is blocked."""
    manager = FakeBrowserManager(_settings(tmp_path))
    # NOT pre-authenticated -> login() runs the form. Simulate the login
    # form POST from the click hook, while the guard is still disarmed.
    manager.page.set_element("#chatbot-container", _populated_application_root())

    login_post = {}

    def on_click(selector: str) -> None:
        if selector == selectors.LOGIN_SUBMIT_BUTTON:
            route = manager.page.simulate_request(
                "POST", selectors.LOGIN_URL, is_navigation=True
            )
            login_post["action"] = route.action
            # login() checks is_authenticated() right after the click.
            manager.page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    manager.page.on_click = on_click
    _patch_bm(monkeypatch, manager)

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=lambda p: None
    )

    assert login_post["action"] == "continue"  # login POST was allowed
    passthrough = report["network_blocker"]["login_phase_passthrough"]
    assert any(p["method"] == "POST" for p in passthrough)
    assert report["network_blocker"]["blocked"] == []  # nothing blocked this run
    assert report["network_blocker"]["armed_before_manual_apply"] is True
    assert report["completed"] is True


# --------------------------------------------------------------------------
# 7. Post-Apply DOM extraction against a fake populated container
# --------------------------------------------------------------------------


def test_extract_application_ui_reads_populated_container():
    page = FakePage()
    page.set_element("#chatbot-container", _populated_application_root())

    result = extract_application_ui(page)

    assert result.application_root_found is True
    assert result.application_root_selector == "#chatbot-container"
    assert result.application_root_outer_html_chars > 0
    assert "resume" in (result.visible_text_excerpt or "").lower()
    assert len(result.controls) == 4
    assert result.resume_selection_control_count == 2  # radio + file upload
    assert result.final_submit_control_count == 1
    assert any(c.looks_like_final_submit and c.tag == "button" for c in result.controls)
    resume_ctl = next(c for c in result.controls if c.element_id == "res1")
    assert resume_ctl.looks_like_resume_control is True
    assert resume_ctl.label_text == "Hemant_Mane.pdf"


def test_extract_application_ui_reports_when_root_absent():
    page = FakePage()  # nothing set
    result = extract_application_ui(page)
    assert result.application_root_found is False
    assert result.controls == []
    assert result.notes and "No POPULATED application root" in result.notes[0]


def test_extract_application_ui_clicks_nothing():
    page = FakePage()
    page.set_element("#chatbot-container", _populated_application_root())

    before = list(page.clicked)
    extract_application_ui(page)
    assert page.clicked == before == []
    assert page.filled == {}


# --------------------------------------------------------------------------
# 8. No application control is clicked by the inspection code (end to end)
# --------------------------------------------------------------------------


def test_no_application_control_clicked_end_to_end(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    clicked_during_extract = {}
    real_wait = {"n": 0}

    def fake_wait(prompt: str) -> None:
        real_wait["n"] += 1
        clicked_during_extract["before"] = list(manager.page.clicked)

    run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    assert real_wait["n"] == 1
    # No click anywhere in the run, and specifically none after the human
    # pause (which is where extraction runs).
    assert manager.page.clicked == []
    assert clicked_during_extract["before"] == []


# --------------------------------------------------------------------------
# 9. Report + artifacts written
# --------------------------------------------------------------------------


def test_report_and_artifacts_written(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=lambda p: None
    )

    out_dir = Path(report["output_dir"])
    assert (out_dir / "report.json").exists()
    assert (out_dir / "01_pre_apply.html").exists()
    assert (out_dir / "02_post_apply.html").exists()
    assert (out_dir / "application_root.html").exists()
    assert "populated" in (out_dir / "application_root.html").read_text(encoding="utf-8")
    assert report["kind"] == "apply_inspection_stage_1_5"
    assert report["completed"] is True


# --------------------------------------------------------------------------
# 10. Isolated / disposable profile
# --------------------------------------------------------------------------


def test_isolated_profile_dir_is_used_by_default(tmp_path, monkeypatch):
    captured = {}

    def factory(settings, profile_dir_override=None):
        captured["profile_dir_override"] = profile_dir_override
        m = FakeBrowserManager(settings, profile_dir_override)
        _make_authenticated(m.page)
        m.page.set_element("#chatbot-container", _populated_application_root())
        return m

    monkeypatch.setattr(apply_module, "BrowserManager", factory)

    run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=lambda p: None
    )

    override = captured["profile_dir_override"]
    assert override is not None
    assert "_apply_session_profiles" in str(override)
    assert str(tmp_path) in str(override)  # under the disposable inspection dir


def test_reuse_session_opts_out_of_isolated_profile(tmp_path, monkeypatch):
    captured = {}

    def factory(settings, profile_dir_override=None):
        captured["profile_dir_override"] = profile_dir_override
        m = FakeBrowserManager(settings, profile_dir_override)
        _make_authenticated(m.page)
        m.page.set_element("#chatbot-container", _populated_application_root())
        return m

    monkeypatch.setattr(apply_module, "BrowserManager", factory)

    run_apply_inspection(
        _settings(tmp_path),
        job_url=TEST_JOB_URL,
        isolated_profile=False,
        wait_for_manual_apply=lambda p: None,
    )

    assert captured["profile_dir_override"] is None


# --------------------------------------------------------------------------
# 11. CAPTCHA/MFA handling unchanged (browser stays open, run resumes)
# --------------------------------------------------------------------------


def test_captcha_during_login_keeps_browser_open_then_resumes(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    seen = {}

    def fake_challenge_wait(prompt: str) -> None:
        seen["closed_during_challenge"] = manager.closed
        seen["prompt"] = prompt
        # Human solves the CAPTCHA and the session becomes authenticated.
        manager.page._elements.pop(selectors.CAPTCHA_INDICATOR, None)
        manager.page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    report = run_apply_inspection(
        _settings(tmp_path),
        job_url=TEST_JOB_URL,
        wait_for_manual_apply=lambda p: None,
        wait_for_manual_completion=fake_challenge_wait,
    )

    assert seen["closed_during_challenge"] is False
    assert "CAPTCHA" in seen["prompt"]
    assert report["completed"] is True
    assert report["network_blocker"]["armed_before_manual_apply"] is True


def test_captcha_pause_keeps_browser_open_and_rechecks_auth_on_enter(tmp_path, monkeypatch):
    """
    (1) CAPTCHA detected -> browser stays open while the terminal waits.
    (2) Enter -> the existing auth recheck runs and login proceeds.
    Ordering is asserted: the pause happens before the guard is armed
    and before the manual-apply pause.
    """
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    events: list[str] = []

    def fake_challenge_wait(prompt: str) -> None:
        assert "CAPTCHA" in prompt
        events.append("captcha_prompt")
        assert manager.closed is False  # browser still open during the wait
        # user solves it in the still-open browser, then presses Enter:
        manager.page._elements.pop(selectors.CAPTCHA_INDICATOR, None)
        manager.page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    report = run_apply_inspection(
        _settings(tmp_path),
        job_url=TEST_JOB_URL,
        wait_for_manual_apply=lambda p: events.append("manual_apply"),
        wait_for_manual_completion=fake_challenge_wait,
    )

    assert events == ["captcha_prompt", "manual_apply"]
    assert report["steps"][0] == {"step": "login", "status": "success"}
    assert report["network_blocker"]["armed_before_manual_apply"] is True
    assert report["completed"] is True


def test_captcha_then_browser_window_closed_yields_clean_report_no_masked_crash(tmp_path, monkeypatch):
    """
    (3) User is shown the CAPTCHA prompt, then closes the browser
    window instead of solving it -> every page access now raises like a
    dead Playwright driver. The run must end with a structured
    report.json (completed False, the connection loss recorded) and
    must NOT propagate a raw crash or a masked secondary
    "Connection closed" from cleanup.
    """
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_bm(monkeypatch, manager)

    def fake_challenge_wait(prompt: str) -> None:
        manager.page.simulate_connection_loss()  # window closed -> driver dead

    report = run_apply_inspection(  # must NOT raise
        _settings(tmp_path),
        job_url=TEST_JOB_URL,
        wait_for_manual_apply=lambda p: None,
        wait_for_manual_completion=fake_challenge_wait,
    )

    assert report["completed"] is False
    assert report.get("error_type") is not None
    assert "Connection closed" in report.get("error", "")
    out_dir = Path(report["output_dir"])
    assert (out_dir / "report.json").exists()
    assert manager.closed is True  # BrowserManager.__exit__ ran without raising
    # the guard lifecycle is untouched: installed once, never unrouted
    assert len(manager.page.routes) == 1
    assert manager.page.unroute_calls == []


def test_mfa_pause_also_stays_human_in_the_loop(tmp_path, monkeypatch):
    """(4) MFA path behaves exactly like CAPTCHA: browser stays open,
    Enter rechecks, run resumes."""
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.MFA_INDICATOR, FakeElement())
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    seen = {}

    def fake_challenge_wait(prompt: str) -> None:
        seen["closed_during_challenge"] = manager.closed
        seen["prompt"] = prompt
        manager.page._elements.pop(selectors.MFA_INDICATOR, None)
        manager.page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())

    report = run_apply_inspection(
        _settings(tmp_path),
        job_url=TEST_JOB_URL,
        wait_for_manual_apply=lambda p: None,
        wait_for_manual_completion=fake_challenge_wait,
    )

    assert seen["closed_during_challenge"] is False
    assert "MFA" in seen["prompt"] or "OTP" in seen["prompt"]
    assert report["completed"] is True


# --------------------------------------------------------------------------
# 12. MutatingRequestBlocker unit-level guarantees
# --------------------------------------------------------------------------


def test_blocker_install_twice_raises():
    page = FakePage()
    blocker = MutatingRequestBlocker()
    blocker.install(page)
    try:
        blocker.install(page)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError on second install()")


def test_blocker_disarmed_passes_everything_but_records_mutating():
    page = FakePage()
    blocker = MutatingRequestBlocker()
    blocker.install(page)

    get_route = page.simulate_request("GET", "https://x/y")
    post_route = page.simulate_request("POST", "https://x/apply")

    assert get_route.action == "continue"
    assert post_route.action == "continue"  # disarmed -> passthrough
    assert [r.method for r in blocker.login_phase_passthrough] == ["POST"]
    assert blocker.blocked == []


def test_blocker_armed_blocks_mutating_keeps_get():
    page = FakePage()
    blocker = MutatingRequestBlocker()
    blocker.install(page)
    blocker.arm()

    get_route = page.simulate_request("GET", "https://x/y")
    post_route = page.simulate_request("POST", "https://x/apply")

    assert get_route.action == "continue"
    assert post_route.action == "abort"
    assert post_route.abort_error_code == "blockedbyclient"
    assert [r.method for r in blocker.blocked] == ["POST"]


# --------------------------------------------------------------------------
# 13. Regression: report.json is ALWAYS written, even when the run stops
#     at / after the manual-Apply pause (the observed failure mode: only
#     01_pre_apply.* produced, no report.json).
# --------------------------------------------------------------------------


def _glob_reports(tmp_path) -> list[Path]:
    return sorted((tmp_path / "inspection_output").glob("apply_*/report.json"))


def test_report_written_when_manual_wait_raises_eoferror(tmp_path, monkeypatch):
    """input() on a non-interactive stdin raises EOFError. That must
    produce a structured report + keep 01_pre_apply, not vanish."""
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    def raise_eof(prompt: str) -> None:
        raise EOFError()

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=raise_eof
    )

    assert report["completed"] is False
    assert report["error_type"] == "NaukriAutomationError"
    assert "interactive terminal" in report["error"]
    out_dir = Path(report["output_dir"])
    assert (out_dir / "report.json").exists()
    assert (out_dir / "01_pre_apply.html").exists()
    assert not (out_dir / "02_post_apply.html").exists()
    # guard was armed before the pause, and is recorded as such
    assert report["network_blocker"]["armed_before_manual_apply"] is True
    assert report["network_blocker"]["armed_final"] is True
    assert report["network_blocker"]["disarmed"] is False
    assert manager.closed is True


def test_report_written_when_manual_wait_raises_keyboardinterrupt(tmp_path, monkeypatch):
    """Ctrl-C during the pause: the report is persisted, then the
    KeyboardInterrupt propagates (not swallowed)."""
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    def raise_ki(prompt: str) -> None:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_apply_inspection(
            _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=raise_ki
        )

    reports = _glob_reports(tmp_path)
    assert len(reports) == 1
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["error_type"] == "KeyboardInterrupt"
    assert data["completed"] is False
    assert data["stopped_at"] == "awaiting_manual_apply"
    assert manager.closed is True  # BrowserManager.__exit__ still ran


def test_partial_report_written_before_manual_apply_returns(tmp_path, monkeypatch):
    """A report.json exists on disk DURING the human pause, so a hard
    kill at that point still leaves a report."""
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    seen = {}

    def fake_wait(prompt: str) -> None:
        reports = _glob_reports(tmp_path)
        seen["count"] = len(reports)
        seen["data"] = json.loads(reports[0].read_text(encoding="utf-8")) if reports else None

    run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    assert seen["count"] == 1
    assert seen["data"]["completed"] is False
    step_names = [s["step"] for s in seen["data"]["steps"]]
    assert "awaiting_manual_apply" in step_names
    assert seen["data"]["network_blocker"]["armed_before_manual_apply"] is True


def test_report_written_when_extraction_raises(tmp_path, monkeypatch):
    """An exception inside extract_application_ui must not lose the
    report or the post-Apply artifacts."""
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    def boom(page):
        raise RuntimeError("boom")

    monkeypatch.setattr(apply_module, "extract_application_ui", boom)

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=lambda p: None
    )

    assert report["completed"] is False
    assert report["error_type"] == "ExtractionError"
    assert "RuntimeError: boom" in report["error"]
    out_dir = Path(report["output_dir"])
    assert (out_dir / "report.json").exists()
    assert (out_dir / "02_post_apply.html").exists()
    assert (out_dir / "application_root.html").exists()


# --------------------------------------------------------------------------
# 14. Bounded waits after manual Apply (no 30s stalls)
# --------------------------------------------------------------------------


def test_post_apply_settle_wait_is_bounded_and_default_timeout_set(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=lambda p: None
    )

    # general auto-wait bound applied right after arming
    assert manager.page.default_timeout_ms == 15000
    # the post-Apply settle used an explicit short timeout, not the default
    assert ("networkidle", 5000) in manager.page.load_state_calls


def test_wait_for_load_state_raising_never_loses_the_report(tmp_path, monkeypatch):
    """A busy Naukri page where every wait_for_load_state times out /
    raises must still yield a report.json — not the observed failure
    mode where the run vanished with only 01_pre_apply.*."""
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    manager.page.wait_for_load_state_error = RuntimeError("Timeout 5000ms exceeded")
    _patch_bm(monkeypatch, manager)

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=lambda p: None
    )

    out_dir = Path(report["output_dir"])
    assert (out_dir / "report.json").exists()
    assert report["network_blocker"]["armed_final"] is True


# --------------------------------------------------------------------------
# 15. Requests blocked during the manual window are surfaced as a note
# --------------------------------------------------------------------------


def test_fresh_profile_form_login_reaches_manual_apply_not_login_timeout(tmp_path, monkeypatch):
    """
    Regression for the first live isolated-profile run: no pre-auth
    indicator -> login() runs the real form; the submit 'redirects' to
    /mnjuser/homepage. The run must classify login as success, ARM the
    guard, and reach the manual-apply pause — NOT stop at 'login' with
    a TimeoutError from wait_for_load_state('networkidle').
    """
    manager = FakeBrowserManager(_settings(tmp_path))
    # NOT authenticated up front -> the form-login path is exercised.

    def redirect_on_submit(selector: str) -> None:
        if selector == selectors.LOGIN_SUBMIT_BUTTON:
            manager.page.url = "https://www.naukri.com/mnjuser/homepage"

    manager.page.on_click = redirect_on_submit
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    reached = {}
    report = run_apply_inspection(
        _settings(tmp_path),
        job_url=TEST_JOB_URL,
        wait_for_manual_apply=lambda p: reached.setdefault("manual_pause", True),
    )

    assert reached.get("manual_pause") is True  # got past login to the human pause
    assert report["steps"][0] == {"step": "login", "status": "success"}
    assert report.get("error_type") is None
    assert report["network_blocker"]["armed_before_manual_apply"] is True
    assert report["completed"] is True
    # login credentials were filled (real form path), submit clicked once
    assert manager.page.filled[selectors.LOGIN_EMAIL_INPUT]
    assert manager.page.clicked.count(selectors.LOGIN_SUBMIT_BUTTON) == 1
    # the login settle never used 'networkidle'
    login_states = [s for s, _t in manager.page.load_state_calls if s == "domcontentloaded"]
    assert "domcontentloaded" in login_states


def test_requests_blocked_during_manual_window_recorded_as_note(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        # the chatbot's own XHRs firing while the human looks at the UI
        manager.page.simulate_request("POST", "https://www.naukri.com/chatbot/questions")
        manager.page.simulate_request("POST", "https://www.naukri.com/chatbot/config")

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    notes = report.get("notes", [])
    assert any("blocked during the manual-apply window" in n for n in notes)
    assert report["network_blocker"]["blocked_count"] == 2
    # guard was NOT weakened — those requests were aborted
    assert report["completed"] is True


# ==========================================================================
# Apply Workflow Inspection: the ONE narrowly-allowlisted apply-init POST.
# ==========================================================================

APPLY_INIT_URL = "https://www.naukri.com" + _APPLY_INIT_PATH


def _armed_blocker_with_allowlist():
    page = FakePage()
    blocker = MutatingRequestBlocker(allow_exact=_APPLY_INIT_ALLOWLIST)
    blocker.install(page)
    blocker.observe_navigation(page)  # framenavigated is page-level
    blocker.arm()
    return page, blocker


def test_allowlist_has_exactly_one_entry_post_apply_init():
    assert _APPLY_INIT_ALLOWLIST == frozenset(
        {("POST", "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply")}
    )


class TargetClosedError(Exception):
    """Local stand-in — Playwright raises one of the same name when the
    page/context closes mid-operation."""


def _allow_and_respond(page, *, url=APPLY_INIT_URL + "?ssoToken=SECRET_TOKEN_VALUE",
                       status=200, content_type="application/json", body=b"{}",
                       body_error=None, status_error=None):
    """Simulate: the browser issues the allowlisted POST (route -> continue,
    request recorded) and its response arrives (response event -> captured)."""
    route = page.simulate_request("POST", url)
    resp = page.simulate_response(
        "POST", url, status=status,
        headers={"content-type": content_type} if content_type else {},
        body=body, body_error=body_error, status_error=status_error,
    )
    return route, resp


def test_apply_init_post_is_allowed_request_and_response_recorded_separately():
    page, blocker = _armed_blocker_with_allowlist()
    body = json.dumps(
        {"formId": "f1", "questions": [], "resumes": [], "authToken": "SEKRIT_VALUE"}
    ).encode("utf-8")

    route, _ = _allow_and_respond(
        page,
        url=APPLY_INIT_URL + "?ssoToken=SECRET_TOKEN_VALUE",
        content_type="application/json; charset=utf-8",
        body=body,
    )

    # request side: allowed through (no route.fetch, no fulfill), recorded
    assert route.action == "continue"
    assert route.fetch_calls == 0
    assert blocker.blocked == []
    assert len(blocker.allowed_apply_init_requests) == 1
    req = blocker.allowed_apply_init_requests[0].model_dump()
    assert req["method"] == "POST"
    assert req["request_index"] == 1
    assert req["url"].endswith(_APPLY_INIT_PATH) and "?" not in req["url"]

    # response side: observed via the response event
    assert len(blocker.apply_init_responses) == 1
    rec = blocker.apply_init_responses[0].model_dump()
    assert rec["method"] == "POST"
    assert rec["status"] == 200
    assert rec["content_type"] == "application/json"  # ";charset" stripped
    assert rec["body_size_bytes"] == len(body)
    assert rec["json_top_level_keys"] == ["authToken", "formId", "questions", "resumes"]
    assert rec["url"].endswith(_APPLY_INIT_PATH) and "?" not in rec["url"]  # query dropped

    dumped = json.dumps(rec)
    assert "SEKRIT_VALUE" not in dumped
    assert "SECRET_TOKEN_VALUE" not in dumped


def test_response_body_is_never_stored_even_for_non_json():
    page, blocker = _armed_blocker_with_allowlist()
    secret = b"<html>token=SUPER_SECRET_VALUE; Set-Cookie: sid=abc</html>"

    _allow_and_respond(page, content_type="text/html", body=secret)

    rec = blocker.apply_init_responses[0].model_dump()
    assert rec["content_type"] == "text/html"
    assert rec["body_size_bytes"] == len(secret)
    assert rec["json_top_level_keys"] == []
    assert rec["json_shape"] is None
    assert "SUPER_SECRET_VALUE" not in json.dumps(rec)


def test_apply_init_allowed_even_when_response_body_unavailable():
    """The request is still allowed and recorded; the response record
    carries status/media-type + a non-sensitive note instead of a body."""
    page, blocker = _armed_blocker_with_allowlist()

    route, _ = _allow_and_respond(
        page, status=200, content_type="application/json",
        body=None, body_error=TargetClosedError("Target page, context or browser has been closed"),
    )

    assert route.action == "continue"
    assert blocker.blocked == []
    assert len(blocker.allowed_apply_init_requests) == 1

    rec = blocker.apply_init_responses[0].model_dump()
    assert rec["status"] == 200
    assert rec["content_type"] == "application/json"
    assert rec["body_size_bytes"] is None  # not safely available
    assert rec["json_shape"] is None
    assert rec["note"] == "body unavailable: TargetClosedError"


def test_response_capture_never_raises_out_of_the_listener():
    """Even a totally broken response object must not propagate."""
    page, blocker = _armed_blocker_with_allowlist()
    page.simulate_request("POST", APPLY_INIT_URL)
    resp = page.simulate_response(
        "POST", APPLY_INIT_URL,
        status_error=RuntimeError("status boom"),
        headers={"content-type": "application/json"},
        body_error=RuntimeError("body boom"),
    )  # must not raise
    rec = blocker.apply_init_responses[0].model_dump()
    assert rec["status"] is None
    assert rec["body_size_bytes"] is None
    assert "status unavailable" in rec["note"]
    assert "body unavailable" in rec["note"]


def test_arbitrary_post_still_blocked_with_allowlist_active():
    page, blocker = _armed_blocker_with_allowlist()
    route = page.simulate_request("POST", "https://www.naukri.com/some/other/endpoint")
    assert route.action == "abort"
    assert [r.method for r in blocker.blocked] == ["POST"]
    assert blocker.apply_init_responses == []


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_other_mutating_methods_blocked_even_on_the_allowlisted_path(method):
    page, blocker = _armed_blocker_with_allowlist()
    route = page.simulate_request(method, APPLY_INIT_URL)  # exact path, wrong method
    assert route.action == "abort"
    assert [r.method for r in blocker.blocked] == [method]
    assert blocker.apply_init_responses == []


def test_hypothetical_final_submit_endpoints_are_blocked():
    page, blocker = _armed_blocker_with_allowlist()
    submit_like = [
        _APPLY_INIT_PATH + "/submit",
        "/cloudgateway-workflow/workflow-services/apply-workflow/v1/submit",
        "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply/confirm",
        "/cloudgateway-workflow/workflow-services/apply-workflow/v2/apply",
        "/cloudgateway-workflow/workflow-services/apply-workflow/v1/finalize",
    ]
    for path in submit_like:
        route = page.simulate_request("POST", "https://www.naukri.com" + path)
        assert route.action == "abort", path
    assert len(blocker.blocked) == len(submit_like)
    assert blocker.apply_init_responses == []


def test_allowlist_is_exact_path_not_substring():
    page, blocker = _armed_blocker_with_allowlist()
    near_misses = [
        "/evil" + _APPLY_INIT_PATH,
        _APPLY_INIT_PATH + "extra",
        _APPLY_INIT_PATH + "/",
        "/x" + _APPLY_INIT_PATH,
        _APPLY_INIT_PATH.replace("/v1/", "/v1x/"),
    ]
    for path in near_misses:
        route = page.simulate_request("POST", "https://naukri.com" + path)
        assert route.action == "abort", path
    assert len(blocker.blocked) == len(near_misses)
    assert blocker.apply_init_responses == []

    ok = page.simulate_request("POST", "https://any.example" + _APPLY_INIT_PATH + "?x=1")
    assert ok.action == "continue"
    assert len(blocker.allowed_apply_init_requests) == 1


def test_allowlist_only_active_while_armed():
    page = FakePage()
    blocker = MutatingRequestBlocker(allow_exact=_APPLY_INIT_ALLOWLIST)
    blocker.install(page)
    route = page.simulate_request("POST", APPLY_INIT_URL)  # not armed yet
    assert route.action == "continue"
    assert blocker.apply_init_responses == []
    assert [r.method for r in blocker.login_phase_passthrough] == ["POST"]


def test_get_requests_still_pass_with_allowlist_active():
    page, blocker = _armed_blocker_with_allowlist()
    route = page.simulate_request(
        "GET", "https://www.naukri.com/static/app.js", resource_type="script"
    )
    assert route.action == "continue"
    assert blocker.blocked == []
    assert blocker.apply_init_responses == []


def test_default_run_reports_single_allowlist_entry_and_captures_init_metadata(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    captured = {}

    def fake_wait(prompt: str) -> None:
        body = json.dumps({"formId": "x", "questions": [], "resumeIds": []}).encode("utf-8")
        url = "https://www.naukri.com" + _APPLY_INIT_PATH
        route = manager.page.simulate_request("POST", url)
        manager.page.simulate_response(
            "POST", url, status=200, headers={"content-type": "application/json"}, body=body
        )
        captured["action"] = route.action
        s = manager.page.simulate_request("POST", url + "/submit")
        captured["submit_action"] = s.action

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    nb = report["network_blocker"]
    assert nb["apply_init_allowlist"] == [
        "POST /cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"
    ]
    assert "exact" in nb["apply_init_allowlist_match"]
    assert captured["action"] == "continue"
    assert captured["submit_action"] == "abort"
    assert nb["apply_init_request_count"] == 1
    assert nb["apply_init_response_count"] == 1
    assert nb["apply_init_responses"][0]["status"] == 200
    assert nb["apply_init_responses"][0]["json_top_level_keys"] == ["formId", "questions", "resumeIds"]
    assert nb["blocked_count"] == 1  # only the /submit attempt
    assert report["completed"] is True


def test_allow_apply_init_false_keeps_pure_default_deny(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    seen = {}

    def fake_wait(prompt: str) -> None:
        route = manager.page.simulate_request(
            "POST", "https://www.naukri.com" + _APPLY_INIT_PATH
        )
        seen["action"] = route.action

    report = run_apply_inspection(
        _settings(tmp_path),
        job_url=TEST_JOB_URL,
        allow_apply_init=False,
        wait_for_manual_apply=fake_wait,
    )

    assert seen["action"] == "abort"
    assert report["network_blocker"]["apply_init_allowlist"] == []
    assert report["network_blocker"]["apply_init_responses"] == []
    assert report["network_blocker"]["blocked_count"] == 1
    assert report["completed"] is True


def test_no_apply_click_or_submit_even_with_init_allowed(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        url = "https://www.naukri.com" + _APPLY_INIT_PATH
        manager.page.simulate_request("POST", url)
        manager.page.simulate_response(
            "POST", url, status=200, headers={"content-type": "application/json"}, body=b"{}"
        )

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    assert manager.page.clicked == []
    assert manager.page.filled == {}
    assert report["completed"] is True


def test_dry_run_default_true_and_apply_inspection_never_touches_it():
    import inspect

    assert Settings(_env_file=None).dry_run is True
    src = inspect.getsource(apply_module).lower()
    assert "dry_run" not in src
    assert "auto_apply" not in src
    assert "prepare_application" not in src
    assert ".click(" not in src  # module issues no clicks


def test_extract_captures_frames_dialogs_and_question_fields():
    page = FakePage()
    page.set_element("#chatbot-container", _populated_application_root())
    page.set_element(
        selectors.FRAME_SELECTOR,
        [
            FakeElement(
                attrs={
                    "src": "https://naukri.com/embed/apply-form?authToken=SECRET",
                    "id": "applyFrame",
                    "name": "apply",
                    "title": "Application form",
                }
            )
        ],
    )
    page.set_element(
        "[role='dialog']",
        [
            FakeElement(
                text="Please answer a few questions",
                attrs={"id": "applyDialog", "role": "dialog", "aria-modal": "true"},
            )
        ],
    )

    result = extract_application_ui(page)

    assert result.question_field_count == 1  # the textarea, not resume/submit
    assert any(c.looks_like_question for c in result.controls)
    assert len(result.frames) == 1
    assert result.frames[0].src == "https://naukri.com/embed/apply-form"  # query dropped
    assert "SECRET" not in json.dumps(result.model_dump())
    assert result.frames[0].element_id == "applyFrame"
    assert len(result.dialogs) >= 1
    assert result.dialogs[0].aria_modal == "true"
    assert result.dialogs[0].element_id == "applyDialog"


# ==========================================================================
# Sanitized recursive JSON shape of the allowlisted apply-init response.
#
# Modelled on the real 200 application/json body (top-level keys:
# applyRedirectUrl, aurusFlow, chatbotResponse, flowType, jobs, ncFlow,
# pzero, skippableQuestions, statusCode). The body itself is NEVER stored.
# ==========================================================================

APPLY_INIT_BODY = {
    "applyRedirectUrl": "https://www.naukri.com/myapply/redirect?jobId=SECRET_JOBID&ssoToken=SEKRIT_TOKEN#f=SEKRIT_FRAG",
    "aurusFlow": False,
    "chatbotResponse": {
        "sessionToken": "eyJhbGciOiJIUzI1Ni.SEKRIT_JWT_PAYLOAD.sig",
        "questions": [
            {"id": "q1", "text": "What is your current CTC?", "type": "TEXT", "mandatory": True},
            {"id": "q2", "text": "Notice period in days?", "type": "NUMBER", "mandatory": False},
        ],
        "greeting": "Hi Hemant Mane, a few quick questions",
    },
    "flowType": "NORMAL",
    "jobs": [{"jobId": "040926008523", "title": "Gen AI Data Scientist", "recruiterEmail": "x@sigma.example"}],
    "ncFlow": {"enabled": True, "candidateName": "Hemant Mane", "candidateEmail": "hemant@example.com"},
    "pzero": None,
    "skippableQuestions": ["q2"],
    "statusCode": 200,
}

_SECRETS_IN_BODY = (
    "SECRET_JOBID",
    "SEKRIT_TOKEN",
    "SEKRIT_FRAG",
    "eyJhbGciOiJIUzI1Ni.SEKRIT_JWT_PAYLOAD",
    "What is your current CTC",
    "Notice period in days",
    "Hi Hemant Mane",
    "Hemant Mane",
    "hemant@example.com",
    "x@sigma.example",
    "040926008523",
    "Gen AI Data Scientist",
)


def test_shape_captures_structure_but_no_secret_values():
    shape, truncated = _json_shape_root(APPLY_INIT_BODY)
    blob = json.dumps(shape)

    for secret in _SECRETS_IN_BODY:
        assert secret not in blob, secret
    assert truncated is False


def test_shape_top_level_matches_observed_keys_and_types():
    shape, _ = _json_shape_root(APPLY_INIT_BODY)
    assert shape["type"] == "object"
    assert shape["key_count"] == 9
    keys = shape["keys"]
    assert set(keys) == {
        "applyRedirectUrl", "aurusFlow", "chatbotResponse", "flowType",
        "jobs", "ncFlow", "pzero", "skippableQuestions", "statusCode",
    }
    assert keys["aurusFlow"] == {"type": "boolean", "value": False}
    assert keys["pzero"] == {"type": "null"}
    assert keys["flowType"] == {"type": "string", "length": 6, "value": "NORMAL"}  # safe enum key
    assert keys["statusCode"] == {"type": "integer", "value": 200}  # safe enum key


def test_shape_applyRedirectUrl_has_scheme_host_path_but_no_query_or_fragment():
    shape, _ = _json_shape_root(APPLY_INIT_BODY)
    node = shape["keys"]["applyRedirectUrl"]
    assert node["type"] == "url"
    assert node["scheme"] == "https"
    assert node["host"] == "www.naukri.com"
    assert node["path"] == "/myapply/redirect"
    assert "query" not in node and "fragment" not in node
    assert "SEKRIT_TOKEN" not in json.dumps(node)
    assert "SEKRIT_FRAG" not in json.dumps(node)


def test_url_shape_helper_drops_query_and_fragment():
    s = "https://host.example/a/b?tok=SECRET&x=1#frag=SECRET2"
    sh = _url_shape(s)
    assert sh == {
        "type": "url", "length": len(s),
        "scheme": "https", "host": "host.example", "path": "/a/b",
    }
    assert "SECRET" not in json.dumps(sh)


def test_shape_nested_objects_and_array_lengths_are_captured():
    shape, _ = _json_shape_root(APPLY_INIT_BODY)
    keys = shape["keys"]

    # chatbotResponse: nested object, question text recorded as length only
    cr = keys["chatbotResponse"]
    assert cr["type"] == "object"
    assert cr["keys"]["sessionToken"] == {"type": "string", "length": len(APPLY_INIT_BODY["chatbotResponse"]["sessionToken"])}
    q = cr["keys"]["questions"]
    assert q["type"] == "array"
    assert q["length"] == 2
    q0 = q["items"][0]
    assert q0["type"] == "object"
    assert set(q0["keys"]) == {"id", "mandatory", "text", "type"}
    assert q0["keys"]["text"]["type"] == "string"
    assert "value" not in q0["keys"]["text"]  # question text NOT recorded
    assert q0["keys"]["mandatory"] == {"type": "boolean", "value": True}

    # jobs: array length + item object shape; ids/emails as lengths only
    jobs = keys["jobs"]
    assert jobs["type"] == "array" and jobs["length"] == 1
    job0 = jobs["items"][0]
    assert set(job0["keys"]) == {"jobId", "recruiterEmail", "title"}
    assert job0["keys"]["jobId"] == {"type": "string", "length": 12}
    assert "value" not in job0["keys"]["recruiterEmail"]

    # ncFlow: nested object; candidate name/email as lengths only
    nc = keys["ncFlow"]
    assert nc["type"] == "object"
    assert nc["keys"]["enabled"] == {"type": "boolean", "value": True}
    assert nc["keys"]["candidateName"]["type"] == "string"
    assert "value" not in nc["keys"]["candidateName"]

    # skippableQuestions: array of strings -> lengths only
    sq = keys["skippableQuestions"]
    assert sq == {"type": "array", "length": 1, "items": [{"type": "string", "length": 2}]}


def test_shape_enforces_depth_width_and_node_budgets():
    # deep nesting -> a max_depth truncation node appears, truncated flag set
    deep: dict = {"leaf": 1}
    for _ in range(20):
        deep = {"child": deep}
    shape, truncated = _json_shape_root(deep)
    assert truncated is True
    assert "max_depth" in json.dumps(shape)

    # very wide object -> keys_omitted recorded, not all 200 keys
    wide = {f"k{i}": i for i in range(200)}
    shape, truncated = _json_shape_root(wide)
    assert truncated is True
    assert shape["key_count"] == 200
    assert shape["keys_omitted"] == 200 - 50
    assert len(shape["keys"]) == 50

    # long array -> only 5 element shapes sampled
    longarr = list(range(1000))
    shape, _ = _json_shape_root(longarr)
    assert shape["type"] == "array"
    assert shape["length"] == 1000
    assert len(shape["items"]) == 5
    assert shape["items_sampled"] == 5

    # huge structure -> node budget hits, truncated node present
    huge = {f"o{i}": {f"n{j}": [1, 2, 3] for j in range(30)} for i in range(30)}
    shape, truncated = _json_shape_root(huge)
    assert truncated is True
    assert "node_budget" in json.dumps(shape)


def test_shape_enum_value_guard_rejects_unsafe_strings():
    # 'flowType' is a safe-enum key, but a long / non-safe-charset value is still not recorded
    shape, _ = _json_shape_root({"flowType": "550e8400-e29b-41d4-a716-446655440000"})  # 36 chars
    assert "value" not in shape["keys"]["flowType"]
    shape, _ = _json_shape_root({"flowType": "has spaces and : punctuation"})
    assert "value" not in shape["keys"]["flowType"]
    # a non-allowlisted key with a short safe value is STILL not recorded
    shape, _ = _json_shape_root({"someCode": "OK"})
    assert "value" not in shape["keys"]["someCode"]


# --- end-to-end through the blocker: identical requests, no body stored ---


def test_apply_init_shape_captured_for_identical_requests_no_body_stored():
    page, blocker = _armed_blocker_with_allowlist()
    body = json.dumps(APPLY_INIT_BODY).encode("utf-8")

    for _ in range(3):  # the live run saw the POST 3 times
        _allow_and_respond(
            page, url=APPLY_INIT_URL + "?ssoToken=SEKRIT_TOKEN",
            content_type="application/json", body=body,
        )

    # request-side and response-side both count 3, in order
    assert [r.request_index for r in blocker.allowed_apply_init_requests] == [1, 2, 3]
    recs = [r.model_dump() for r in blocker.apply_init_responses]
    assert [r["request_index"] for r in recs] == [1, 2, 3]
    assert all(r["body_size_bytes"] == len(body) for r in recs)
    assert recs[0]["status"] == 200
    assert recs[0]["json_shape"]["keys"]["applyRedirectUrl"]["type"] == "url"
    assert recs[0]["json_shape_truncated"] is False

    blob = json.dumps(recs + [r.model_dump() for r in blocker.allowed_apply_init_requests])
    for secret in _SECRETS_IN_BODY + ("SEKRIT_TOKEN",):
        assert secret not in blob, secret

    from naukri_agent.browser.models import AppInitResponseRecord

    assert set(AppInitResponseRecord.model_fields) == {
        "request_index", "seq", "method", "url", "status", "content_type",
        "body_size_bytes", "json_top_level_keys", "json_shape",
        "json_shape_truncated", "note",
    }


def test_request_and_response_counts_diverge_when_a_response_is_lost():
    """3 allowed requests but only 2 responses observed (one lost to a
    context close) -> the two counts differ; that mismatch is the
    diagnostic signal, not a crash."""
    page, blocker = _armed_blocker_with_allowlist()
    body = json.dumps(APPLY_INIT_BODY).encode("utf-8")

    for i in range(3):
        page.simulate_request("POST", APPLY_INIT_URL)
        if i < 2:
            page.simulate_response(
                "POST", APPLY_INIT_URL, status=200,
                headers={"content-type": "application/json"}, body=body,
            )

    assert len(blocker.allowed_apply_init_requests) == 3
    assert len(blocker.apply_init_responses) == 2
    assert [r.request_index for r in blocker.apply_init_responses] == [1, 2]


def test_shape_capture_does_not_widen_the_allowlist():
    """A DIFFERENT endpoint returning rich JSON is still blocked; its
    response event is ignored — capture is exclusive to the one
    allowlisted (POST, path)."""
    page, blocker = _armed_blocker_with_allowlist()

    for path in (
        _APPLY_INIT_PATH + "/submit",
        "/cloudgateway-workflow/workflow-services/apply-workflow/v1/confirm",
        _APPLY_INIT_PATH.replace("/v1/", "/v2/"),
    ):
        url = "https://www.naukri.com" + path
        route = page.simulate_request("POST", url)
        assert route.action == "abort", path
        assert route.fetch_calls == 0, path
        # even if a response event fires for it, it is not captured
        page.simulate_response(
            "POST", url, status=200,
            headers={"content-type": "application/json"}, body=b'{"secret":"X"}',
        )

    assert blocker.apply_init_responses == []
    assert blocker.allowed_apply_init_requests == []
    assert len(blocker.blocked) == 3


def test_on_response_ignores_events_before_arm_and_for_other_urls():
    page = FakePage()
    blocker = MutatingRequestBlocker(allow_exact=_APPLY_INIT_ALLOWLIST)
    blocker.install(page)

    # pre-arm: ignored
    page.simulate_response("POST", APPLY_INIT_URL, status=200,
                           headers={"content-type": "application/json"}, body=b"{}")
    assert blocker.apply_init_responses == []

    blocker.arm()
    # armed but wrong URL: ignored
    page.simulate_response("POST", "https://www.naukri.com/other", status=200,
                           headers={"content-type": "application/json"}, body=b"{}")
    # armed but wrong method on the right path: ignored
    page.simulate_response("GET", APPLY_INIT_URL, status=200,
                           headers={"content-type": "application/json"}, body=b"{}")
    assert blocker.apply_init_responses == []

    # armed + exact match: captured
    page.simulate_response("POST", APPLY_INIT_URL, status=200,
                           headers={"content-type": "application/json"}, body=b'{"k":1}')
    assert len(blocker.apply_init_responses) == 1


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_shape_mode_still_blocks_other_methods_on_the_allowlisted_path(method):
    page, blocker = _armed_blocker_with_allowlist()
    route = page.simulate_request(method, APPLY_INIT_URL)
    assert route.action == "abort"
    assert route.fetch_calls == 0
    assert blocker.apply_init_responses == []
    assert [r.method for r in blocker.blocked] == [method]


# ==========================================================================
# Post-init transition inspection (READ-ONLY): what happens right AFTER the
# allowed Apply-init response. Observes GET requests, non-apply-init
# responses, and frame navigations; flags anything touching /myapply/saveApply.
# Never allows or blocks anything.
# ==========================================================================

SAVE_APPLY_URL = "https://www.naukri.com/myapply/saveApply"


def test_get_response_and_navigation_after_apply_init_are_observed():
    page, blocker = _armed_blocker_with_allowlist()
    _allow_and_respond(page)  # sets the post-init marker

    page.simulate_request(
        "GET", "https://www.naukri.com/myapply/page?x=1", resource_type="document"
    )
    resp = page.simulate_response(
        "GET", "https://www.naukri.com/myapply/page", status=200,
        headers={"content-type": "text/html"}, resource_type="document",
    )
    page.simulate_navigation("https://www.naukri.com/myapply/page")

    kinds = [e.kind for e in blocker.post_init_events]
    assert "get-request" in kinds and "response" in kinds and "navigation" in kinds
    getr = next(e for e in blocker.post_init_events if e.kind == "get-request")
    assert getr.method == "GET"
    assert getr.url == "https://www.naukri.com/myapply/page"  # query dropped
    assert getr.after_apply_init is True
    assert resp.body_calls == 0  # post-init responses: metadata only, body never read


def test_events_before_apply_init_are_not_recorded_as_post_init():
    page, blocker = _armed_blocker_with_allowlist()
    page.simulate_request("GET", "https://www.naukri.com/before?a=1", resource_type="document")
    page.simulate_navigation("https://www.naukri.com/before")
    assert blocker.post_init_events == []
    assert blocker.save_apply_events == []
    assert blocker.apply_init_marker_seq is None


def test_save_apply_get_normalized_query_names_only_no_values():
    page, blocker = _armed_blocker_with_allowlist()
    _allow_and_respond(page)

    page.simulate_request(
        "GET",
        SAVE_APPLY_URL + "?candId=SECRET_CAND&jobId=SECRET_JOB&flag#frag=SECRET_FRAG",
        resource_type="document",
    )

    assert len(blocker.save_apply_events) == 1
    ev = blocker.save_apply_events[0].model_dump()
    assert ev["url"] == SAVE_APPLY_URL  # scheme/host/path only
    assert ev["query_param_keys"] == ["candId", "jobId", "flag"]
    assert ev["is_save_apply"] is True
    blob = json.dumps([e.model_dump() for e in blocker.post_init_events])
    for secret in ("SECRET_CAND", "SECRET_JOB", "SECRET_FRAG"):
        assert secret not in blob


def test_navigation_to_save_apply_is_flagged():
    page, blocker = _armed_blocker_with_allowlist()
    _allow_and_respond(page)

    page.simulate_navigation(SAVE_APPLY_URL + "?applicationId=SECRET_APP")

    assert len(blocker.save_apply_events) == 1
    ev = blocker.save_apply_events[0]
    assert ev.kind == "navigation"
    assert ev.is_navigation is True
    assert ev.url == SAVE_APPLY_URL
    assert ev.is_save_apply is True
    assert "SECRET_APP" not in json.dumps(ev.model_dump())


def test_post_init_response_persists_no_headers_or_body():
    page, blocker = _armed_blocker_with_allowlist()
    _allow_and_respond(page)

    resp = page.simulate_response(
        "GET", "https://www.naukri.com/myapply/x", status=302,
        headers={
            "content-type": "text/html",
            "set-cookie": "sid=SECRET_COOKIE",
            "authorization": "Bearer SECRET_TOKEN",
        },
        body=b"<html>SECRET_BODY</html>",
        resource_type="document",
    )

    ev = next(e for e in blocker.post_init_events if e.kind == "response")
    d = ev.model_dump()
    assert d["status"] == 302
    assert d["content_type"] == "text/html"
    assert set(d) == {
        "seq", "kind", "method", "url", "query_param_keys", "resource_type",
        "is_navigation", "status", "content_type", "is_save_apply",
        "after_apply_init", "note",
    }
    assert "SECRET" not in json.dumps(d)
    assert resp.body_calls == 0  # body never read for post-init responses


def test_blocked_post_after_apply_init_stays_blocked_and_allowlist_not_widened():
    page, blocker = _armed_blocker_with_allowlist()
    _allow_and_respond(page)
    assert blocker.allow_exact == _APPLY_INIT_ALLOWLIST

    r1 = page.simulate_request("POST", "https://www.naukri.com/anything")
    r2 = page.simulate_request("POST", SAVE_APPLY_URL + "?x=1")
    assert r1.action == "abort"
    assert r2.action == "abort"

    assert blocker.allow_exact == _APPLY_INIT_ALLOWLIST  # unchanged
    assert len(blocker.allowed_apply_init_requests) == 1  # no new allowed request
    assert [b.method for b in blocker.blocked] == ["POST", "POST"]
    flagged = [e for e in blocker.post_init_events if e.kind == "blocked-mutation"]
    assert len(flagged) == 1 and flagged[0].is_save_apply is True
    assert any(e.is_save_apply for e in blocker.save_apply_events)


def test_event_ordering_around_apply_init_is_monotonic():
    page, blocker = _armed_blocker_with_allowlist()
    _allow_and_respond(page)
    page.simulate_request("GET", "https://www.naukri.com/myapply/after", resource_type="document")
    page.simulate_navigation(SAVE_APPLY_URL)

    req_seq = blocker.allowed_apply_init_requests[0].seq
    resp_seq = blocker.apply_init_responses[0].seq
    post_seqs = [e.seq for e in blocker.post_init_events]

    assert req_seq < resp_seq < min(post_seqs)
    assert post_seqs == sorted(post_seqs)
    assert blocker.apply_init_marker_seq == req_seq


def test_navigation_observer_survives_frame_url_raising():
    page, blocker = _armed_blocker_with_allowlist()
    _allow_and_respond(page)

    page.simulate_navigation("ignored", url_error=TargetClosedError("context closed"))

    navs = [e for e in blocker.post_init_events if e.kind == "navigation"]
    assert len(navs) == 1
    assert navs[0].url == "<unavailable>"
    assert "frame.url unavailable: TargetClosedError" in navs[0].note


def test_post_init_response_status_unavailable_is_a_note_not_a_crash():
    page, blocker = _armed_blocker_with_allowlist()
    _allow_and_respond(page)

    page.simulate_response(
        "GET", "https://www.naukri.com/myapply/x", resource_type="document",
        status_error=RuntimeError("status boom"), headers={"content-type": "text/html"},
    )

    ev = next(e for e in blocker.post_init_events if e.kind == "response")
    assert ev.status is None
    assert "status unavailable" in ev.note


def test_static_asset_gets_after_apply_init_are_not_recorded():
    page, blocker = _armed_blocker_with_allowlist()
    _allow_and_respond(page)
    page.simulate_request("GET", "https://www.naukri.com/img/logo.png", resource_type="image")
    page.simulate_request("GET", "https://www.naukri.com/app.css", resource_type="stylesheet")
    assert blocker.post_init_events == []


def test_run_report_exposes_post_init_transition_fields(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    _patch_bm(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        url = "https://www.naukri.com" + _APPLY_INIT_PATH
        manager.page.simulate_request("POST", url)
        manager.page.simulate_response(
            "POST", url, status=200, headers={"content-type": "application/json"},
            body=json.dumps({"applyRedirectUrl": "/myapply/saveApply"}).encode("utf-8"),
        )
        manager.page.simulate_request(
            "GET", "https://www.naukri.com/myapply/saveApply?id=SECRET_ID", resource_type="document"
        )
        manager.page.simulate_navigation("https://www.naukri.com/myapply/saveApply")

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )
    nb = report["network_blocker"]
    assert nb["save_apply_seen"] is True
    assert nb["save_apply_watch_path"] == "/myapply/saveApply"
    assert nb["post_init_event_count"] >= 2
    assert any(e["is_save_apply"] for e in nb["save_apply_events"])
    assert nb["apply_init_marker_seq"] is not None
    assert "SECRET_ID" not in json.dumps(nb["post_init_events"])
    assert nb["apply_init_allowlist"] == [
        "POST /cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"
    ]
    assert report["completed"] is True


# ==========================================================================
# Post-init client-workflow observation (READ-ONLY, PASSIVE): why the
# questionnaire returned by Apply-init does not visibly render.
# ==========================================================================


def _armed_client_observer():
    page = FakePage()
    blocker = MutatingRequestBlocker(allow_exact=_APPLY_INIT_ALLOWLIST)
    blocker.install(page)
    blocker.observe_navigation(page)
    obs = PostInitClientObserver(blocker)
    obs.install(page)
    blocker.arm()
    page.simulate_request("POST", APPLY_INIT_URL)  # -> post-init window is now active
    return page, blocker, obs


_DRAIN_KEY = "window.__naukriObs.drain()"


def _drain_result(**over):
    base = {
        "added": 0, "removed": 0, "attrs": 0, "chardata": 0,
        "addTags": {}, "remTags": {}, "locs": [],
        "open_shadow_roots": 0, "shadow_hosts": [], "window_ms": 100,
    }
    base.update(over)
    return base


def test_redact_console_text_strips_urls_emails_tokens_and_caps():
    out = _redact_console_text(
        "NetworkError: fetch https://x.com/a/b?token=SEKRIT#frag failed for user a@b.com "
        "jwt eyJhbGciOiJI.eyJzdWIiOiIxMjM0NTY.SflKxw\n at foo (https://x.com/j.js:1:2)"
    )
    assert "SEKRIT" not in out
    assert "a@b.com" not in out
    assert "eyJhbGciOiJI.eyJzdWIiOiIxMjM0NTY" not in out
    assert "https://x.com/a/b" in out  # URL kept as path
    assert "\n" not in out and len(out) <= 300
    assert out.startswith("NetworkError:")  # diagnostic prefix preserved


def test_client_observation_does_not_touch_the_mutation_allowlist():
    page, blocker, obs = _armed_client_observer()
    assert blocker.allow_exact == _APPLY_INIT_ALLOWLIST

    for path in (
        "/myapply/saveApply",
        _APPLY_INIT_PATH + "/answer",
        _APPLY_INIT_PATH + "/next",
        _APPLY_INIT_PATH + "/continue",
        _APPLY_INIT_PATH + "/submit",
        _APPLY_INIT_PATH + "/confirm",
        _APPLY_INIT_PATH + "/save",
    ):
        r = page.simulate_request("POST", "https://www.naukri.com" + path)
        assert r.action == "abort", path

    assert blocker.allow_exact == _APPLY_INIT_ALLOWLIST  # unchanged
    assert len(blocker.allowed_apply_init_requests) == 1  # observers created no allowed requests
    assert obs.console_events == [] and obs.page_errors == []


def test_console_and_pageerror_capture_is_bounded_and_secret_free():
    page, blocker, obs = _armed_client_observer()

    page.simulate_pageerror(
        "SecurityError: blocked https://www.naukri.com/api/x?token=SEKRIT_TOKEN&sid=SEKRIT "
        "cookie=abc Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.PAYLOAD.SIG\n  at stack line 2"
    )
    page.simulate_console("z" * 5000, level="error")
    page.simulate_console("noisy log with value 42", level="log")

    perr = obs.page_errors[0].model_dump()
    blob = json.dumps([r.model_dump() for r in obs.page_errors + obs.console_events])
    for secret in ("SEKRIT_TOKEN", "cookie=abc", "eyJhbGciOiJIUzI1NiJ9.PAYLOAD", "Bearer eyJ"):
        assert secret not in blob

    assert perr["kind"] == "pageerror" and perr["level"] == "error"
    assert perr["message_length"] > len(perr["message_excerpt"])  # full length kept, not full text
    assert "\n" not in perr["message_excerpt"] and len(perr["message_excerpt"]) <= 300
    assert perr["message_excerpt"].startswith("SecurityError:")  # diagnostic prefix preserved

    err = next(r for r in obs.console_events if r.level == "error")
    log = next(r for r in obs.console_events if r.level == "log")
    assert err.message_length == 5000 and len(err.message_excerpt) <= 300
    assert log.message_excerpt is None and log.message_length == len("noisy log with value 42")

    assert set(ConsoleEventRecord.model_fields) == {
        "seq", "kind", "level", "message_length", "message_excerpt",
        "url", "query_param_keys", "line", "note",
    }


def test_console_capture_normalizes_source_url_and_keeps_query_names_only():
    page, blocker, obs = _armed_client_observer()
    page.simulate_console(
        "boom", level="error",
        url="https://www.naukri.com/chatbot.js?v=abc123&sig=SEKRIT", line=42,
    )
    rec = obs.console_events[0].model_dump()
    assert rec["url"] == "https://www.naukri.com/chatbot.js"
    assert rec["query_param_keys"] == ["v", "sig"]
    assert rec["line"] == 42
    assert "SEKRIT" not in json.dumps(rec) and "abc123" not in json.dumps(rec)


def test_mutation_observer_capture_is_aggregate_only():
    page, blocker, obs = _armed_client_observer()
    page.on_evaluate = lambda e: (
        _drain_result(
            added=40, removed=5, attrs=12, chardata=3,
            addTags={"div": 30, "input": 10}, remTags={"span": 5},
            locs=["body", "div#chatbot-container"],
            open_shadow_roots=2, shadow_hosts=["naukri-chat#root", "div#w"],
            window_ms=4200,
        )
        if _DRAIN_KEY in e else None
    )

    obs.drain_dom_mutations(page)
    rec = obs.dom_mutation_summaries[0].model_dump()
    assert rec["added_nodes"] == 40 and rec["removed_nodes"] == 5
    assert rec["added_tag_histogram"] == {"div": 30, "input": 10}
    assert rec["attach_locations"] == ["body", "div#chatbot-container"]
    assert rec["open_shadow_roots"] == 2
    assert rec["shadow_host_summaries"] == ["naukri-chat#root", "div#w"]
    assert rec["window_ms"] == 4200

    assert set(DomMutationSummary.model_fields) == {
        "seq", "added_nodes", "removed_nodes", "attribute_changes",
        "character_data_changes", "added_tag_histogram", "removed_tag_histogram",
        "attach_locations", "open_shadow_roots", "shadow_host_summaries",
        "window_ms", "note",
    }


def test_repeated_dom_sampling_detects_transient_ui():
    page, blocker, obs = _armed_client_observer()
    page.set_element("#chatbot-container", _populated_application_root())
    obs.sample_dom(page, 0)  # UI present
    page._elements.pop("#chatbot-container", None)
    obs.sample_dom(page, 1)  # UI gone

    s0, s1 = obs.dom_samples[0], obs.dom_samples[1]
    assert s0.application_root_found is True and s0.question_field_count >= 1
    assert s1.application_root_found is False
    assert [s.sample_index for s in obs.dom_samples] == [0, 1]
    assert s0.seq < s1.seq

    assert set(DomSampleRecord.model_fields) == {
        "seq", "sample_index", "application_root_found", "application_root_selector",
        "control_count", "question_field_count", "resume_selection_control_count",
        "final_submit_control_count", "frame_count", "dialog_count",
        "outer_html_chars", "note",
    }


def test_iframe_attach_detach_lifecycle_is_captured_path_only():
    page, blocker, obs = _armed_client_observer()
    page.simulate_frame_attached("https://www.naukri.com/embed/chat?authToken=SEK&x=1")
    page.simulate_frame_detached("https://www.naukri.com/embed/chat?authToken=SEK")

    evs = [e.model_dump() for e in obs.frame_lifecycle]
    assert [e["event"] for e in evs] == ["attached", "detached"]
    assert evs[0]["url"] == "https://www.naukri.com/embed/chat"  # query dropped
    assert evs[0]["query_param_keys"] == ["authToken", "x"]  # names only
    assert "SEK" not in json.dumps(evs)
    assert evs[0]["seq"] < evs[1]["seq"]
    assert set(FrameLifecycleRecord.model_fields) == {
        "seq", "event", "url", "query_param_keys", "is_main_frame", "note",
    }


def test_websocket_metadata_only_never_payloads():
    page, blocker, obs = _armed_client_observer()
    ws = page.simulate_websocket("wss://chat.naukri.com/socket?token=SEKRIT_WS")
    ws.simulate_frame("sent", "SECRET_OUTGOING_PAYLOAD")
    ws.simulate_frame("received", b"SECRET_INCOMING_BYTES_PAYLOAD")
    ws.simulate_close()

    rec = obs.websocket_events[0].model_dump()
    assert rec["url"] == "wss://chat.naukri.com/socket"
    assert rec["query_param_keys"] == ["token"]
    assert rec["frames_sent"] == 1 and rec["frames_received"] == 1
    assert rec["bytes_sent"] == len("SECRET_OUTGOING_PAYLOAD")
    assert rec["bytes_received"] == len(b"SECRET_INCOMING_BYTES_PAYLOAD")
    assert rec["closed"] is True

    blob = json.dumps([r.model_dump() for r in obs.websocket_events])
    for secret in ("SEKRIT_WS", "SECRET_OUTGOING_PAYLOAD", "SECRET_INCOMING_BYTES_PAYLOAD"):
        assert secret not in blob

    assert set(WebSocketEventRecord.model_fields) == {
        "seq", "url", "query_param_keys", "opened", "closed",
        "frames_sent", "frames_received", "bytes_sent", "bytes_received", "note",
    }


def test_client_observation_sequence_ordering_is_monotonic_and_anchored():
    page, blocker, obs = _armed_client_observer()
    marker = blocker.apply_init_marker_seq
    assert marker is not None

    page.simulate_pageerror("TypeError: x")
    page.on_evaluate = lambda e: _drain_result(added=1) if _DRAIN_KEY in e else None
    obs.drain_dom_mutations(page)
    page.simulate_frame_attached("https://h/x")
    obs.sample_dom(page, 0)

    seqs = [
        obs.page_errors[0].seq,
        obs.dom_mutation_summaries[0].seq,
        obs.frame_lifecycle[0].seq,
        obs.dom_samples[0].seq,
    ]
    assert all(s > marker for s in seqs)
    assert seqs == sorted(seqs)


def test_observers_ignore_events_before_the_post_init_window():
    page = FakePage()
    blocker = MutatingRequestBlocker(allow_exact=_APPLY_INIT_ALLOWLIST)
    blocker.install(page)
    obs = PostInitClientObserver(blocker)
    obs.install(page)
    blocker.arm()  # armed, but NO apply-init request yet -> not post-init-active

    page.simulate_pageerror("early error")
    page.simulate_console("early", level="error")
    page.simulate_websocket("wss://x/y")
    page.simulate_frame_attached("https://x/z")
    obs.drain_dom_mutations(page)

    assert obs.page_errors == []
    assert obs.console_events == []
    assert obs.websocket_events == []
    assert obs.frame_lifecycle == []
    assert obs.dom_mutation_summaries == []


def test_client_observation_collections_are_capped():
    page, blocker, obs = _armed_client_observer()
    for i in range(_CLIENT_OBS_MAX + 25):
        page.simulate_pageerror(f"E{i}")
    assert len(obs.page_errors) == _CLIENT_OBS_MAX
    assert obs.caps.get("page_errors", 0) >= 25


def test_observer_failures_never_abort_the_run(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    manager.page.on_evaluate = lambda e: (_ for _ in ()).throw(RuntimeError("evaluate boom"))
    _patch_bm(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        url = "https://www.naukri.com" + _APPLY_INIT_PATH
        manager.page.simulate_request("POST", url)
        manager.page.simulate_response(
            "POST", url, status=200, headers={"content-type": "application/json"}, body=b"{}"
        )
        manager.page.simulate_pageerror("TypeError: boom")

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    assert report["completed"] is True
    co = report["post_init_client_observations"]
    assert any("evaluate" in n.lower() for n in co["observer_notes"])
    assert (Path(report["output_dir"]) / "report.json").exists()


def test_run_report_exposes_client_observations_and_bounded_sampling(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _make_authenticated(manager.page)
    manager.page.set_element("#chatbot-container", _populated_application_root())
    manager.page.on_evaluate = lambda e: _drain_result(added=2, locs=["body"]) if _DRAIN_KEY in e else None
    _patch_bm(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        url = "https://www.naukri.com" + _APPLY_INIT_PATH
        manager.page.simulate_request("POST", url)
        manager.page.simulate_response(
            "POST", url, status=200, headers={"content-type": "application/json"},
            body=b'{"applyRedirectUrl":"/myapply/saveApply","flowType":"default"}',
        )
        manager.page.simulate_pageerror("ReferenceError: naukriChat is not defined")

    report = run_apply_inspection(
        _settings(tmp_path), job_url=TEST_JOB_URL, wait_for_manual_apply=fake_wait
    )

    assert manager.page.clicked == []
    assert manager.page.filled == {}
    co = report["post_init_client_observations"]
    assert co["counts"]["page_errors"] == 1
    assert co["counts"]["dom_samples"] == _DOM_SAMPLE_COUNT
    assert co["counts"]["dom_mutation_summaries"] >= _DOM_SAMPLE_COUNT
    assert co["max_per_collection"] == _CLIENT_OBS_MAX
    assert co["dom_sample_plan"] == {"count": _DOM_SAMPLE_COUNT, "interval_ms": _DOM_SAMPLE_INTERVAL_MS}
    # bounded: exactly (count-1) sampling intervals, all the fixed interval
    assert manager.page.wait_for_timeout_calls == [_DOM_SAMPLE_INTERVAL_MS] * (_DOM_SAMPLE_COUNT - 1)
    # allowlist still exactly one entry; nothing widened
    assert report["network_blocker"]["apply_init_allowlist"] == [
        "POST /cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"
    ]
    # the injected observer script was registered for future documents
    assert any("MutationObserver" in s for s in manager.page.init_scripts)
    assert report["completed"] is True
