"""
browser/profile_inspection.py: the prerequisite read-only inspection
for a future resume/profile "touch" feature. Mirrors
test_browser_inspection.py's CAPTCHA/MFA mocked-browser pattern.
"""

from __future__ import annotations

import inspect

from naukri_agent.browser import selectors
from naukri_agent.browser.profile_inspection import run_profile_edit_inspection
from naukri_agent.config import Settings

from .browser_fakes import FakeElement, FakePage


class FakeBrowserManager:
    def __init__(self, settings, profile_dir_override=None) -> None:
        self.settings = settings
        self.profile_dir_override = profile_dir_override
        self.page = FakePage()
        self.closed = False

    def __enter__(self) -> "FakeBrowserManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.closed = True


def _settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        _env_file=None, naukri_email="a@b.com", naukri_password="pw",
        inspection_output_dir=tmp_path / "inspection_output",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _patch_browser_manager(monkeypatch, manager: FakeBrowserManager) -> None:
    import naukri_agent.browser.profile_inspection as module

    monkeypatch.setattr(module, "BrowserManager", lambda settings, profile_dir_override=None: manager)


def _clear_login_and_succeed(page: FakePage) -> None:
    page.url = "https://www.naukri.com/mnjuser/homepage"
    page.set_element(selectors.AUTHENTICATED_NAV_INDICATOR, FakeElement())


def test_captcha_pause_reuses_stage_1_handling(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    manager.page.set_element(selectors.CAPTCHA_INDICATOR, FakeElement())
    _patch_browser_manager(monkeypatch, manager)

    def fake_wait(prompt: str) -> None:
        manager.page._elements.pop(selectors.CAPTCHA_INDICATOR, None)
        _clear_login_and_succeed(manager.page)

    report = run_profile_edit_inspection(_settings(tmp_path), wait_for_manual_completion=fake_wait)
    assert report["completed"] is True
    assert manager.closed is True


def test_defaults_to_isolated_profile():
    import inspect as _inspect

    sig = _inspect.signature(run_profile_edit_inspection)
    assert sig.parameters["isolated_profile"].default is True


def test_reads_the_profile_edit_page_without_mutating_anything(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _clear_login_and_succeed(manager.page)
    manager.page.set_element(
        selectors.PROFILE_EDIT_SECTION_ROOT_CANDIDATES[1],  # "form"
        [FakeElement(children={
            "input, select, textarea, button": [
                FakeElement(tag="input", attrs={"name": "fullName", "type": "text"}),
                FakeElement(tag="button", text="Save", attrs={}),
            ]
        })],
    )
    _patch_browser_manager(monkeypatch, manager)

    report = run_profile_edit_inspection(_settings(tmp_path), wait_for_manual_completion=lambda p: None)

    assert report["completed"] is True
    assert selectors.PROFILE_EDIT_URL in manager.page.goto_calls
    step = next(s for s in report["steps"] if s["step"] == "profile_edit_ui")
    assert len(step["result"]["controls"]) == 2
    assert len(step["result"]["save_control_candidates"]) == 1
    # Strictly read-only: no fill, no click beyond the implicit
    # goto() navigation -- the module never interacts with a control.
    assert manager.page.filled == {}
    assert manager.page.clicked == []


def test_no_matching_section_root_reports_a_note(tmp_path, monkeypatch):
    manager = FakeBrowserManager(_settings(tmp_path))
    _clear_login_and_succeed(manager.page)
    _patch_browser_manager(monkeypatch, manager)

    report = run_profile_edit_inspection(_settings(tmp_path), wait_for_manual_completion=lambda p: None)
    step = next(s for s in report["steps"] if s["step"] == "profile_edit_ui")
    assert step["result"]["section_roots_found"] == []
    assert step["result"]["notes"]


def test_module_never_calls_a_mutating_page_method():
    """Structural guard: this module must only ever read — never
    fill/click/select anything (goto for navigation is the one
    exception, same as every other read-only inspection tool)."""
    import naukri_agent.browser.profile_inspection as module

    src = inspect.getsource(module)
    assert ".fill(" not in src
    assert ".click(" not in src
    assert ".check(" not in src
    assert ".select_option(" not in src
