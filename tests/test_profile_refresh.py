"""Daily Naukri profile refresh by resume rotation: rotation, gates, recording, and the upload itself."""

from __future__ import annotations

import datetime
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import naukri_agent.cli.main as cli_main
from naukri_agent.browser import selectors
from naukri_agent.browser.exceptions import NaukriCaptchaError
from naukri_agent.browser.models import ResumeUploadResult
from naukri_agent.browser.profile import upload_resume
from naukri_agent.config import Settings
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import ResumeRefresh
from naukri_agent.orchestration.profile_refresh import pick_next_resume, run_profile_refresh

from .digest_fakes import in_memory_factory

UTC = datetime.UTC
IDS = ["data_analyst", "data_scientist", "ai_ml_engineer"]
MORNING = datetime.datetime(2026, 10, 9, 4, 30, tzinfo=UTC)  # 10:00 IST on 9 Oct


def make_settings(tmp_path, *, enabled=True, files=True, **over) -> Settings:
    lines = ["resumes:"]
    for rid in IDS:
        f = tmp_path / "resumes" / f"{rid}.pdf"
        if files:
            f.parent.mkdir(exist_ok=True)
            f.write_bytes(rid.encode())
        lines += [f"  - id: {rid}", f"    file: {f.as_posix()}", "    roles: []"]
    reg = tmp_path / "resumes.yaml"
    reg.write_text("\n".join(lines), encoding="utf-8")
    return Settings(
        _env_file=None, resume_registry_path=reg, profile_refresh_enabled=enabled,
        profile_refresh_pause_file=tmp_path / "PAUSE_PROFILE_REFRESH", **over,
    )


class FakeClient:
    def __init__(self, result=None, error=None):
        self.uploaded = []
        self._result = result
        self._error = error

    def upload_resume(self, path):
        self.uploaded.append(path.name)
        if self._error:
            raise self._error
        return self._result or ResumeUploadResult(
            before_filename="old.pdf", after_filename=path.name, verified=True
        )


def opener(client, opened=None):
    @contextmanager
    def _open(_settings):
        if opened is not None:
            opened.append(1)
        yield client

    return _open


def go(settings, factory, client, *, now=MORNING, execute=True, notes=None):
    opened = []
    r = run_profile_refresh(
        settings, execute=execute, session_factory=factory, open_client=opener(client, opened),
        now=now, notify=(notes.append if notes is not None else None),
    )
    return r, opened


def rows(factory):
    with session_scope(factory) as s:
        return [(r.resume_id, r.outcome) for r in s.query(ResumeRefresh).order_by(ResumeRefresh.id)]


# --- rotation ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "last, expected",
    [(None, "data_analyst"), ("data_analyst", "data_scientist"), ("data_scientist", "ai_ml_engineer"),
     ("ai_ml_engineer", "data_analyst"), ("removed_one", "data_analyst")],
)
def test_pick_next_resume_rotates_in_registry_order_and_wraps(last, expected):
    entries = [SimpleNamespace(id=i) for i in IDS]
    assert pick_next_resume(entries, last).id == expected


def test_pick_next_resume_with_no_resumes_is_none():
    assert pick_next_resume([], None) is None


def test_it_rotates_one_resume_per_day_across_days(tmp_path):
    cfg, factory, client = make_settings(tmp_path), in_memory_factory(), FakeClient()
    for day in range(4):
        r, _ = go(cfg, factory, client, now=MORNING + datetime.timedelta(days=day))
        assert r.outcome == "uploaded"
    assert client.uploaded == ["data_analyst.pdf", "data_scientist.pdf", "ai_ml_engineer.pdf", "data_analyst.pdf"]


# --- gates -------------------------------------------------------------------------------------------------------


def test_dry_run_names_the_next_resume_and_touches_nothing(tmp_path):
    cfg, factory = make_settings(tmp_path, enabled=False), in_memory_factory()
    r, opened = go(cfg, factory, FakeClient(), execute=False)
    assert r.outcome == "would_upload" and r.resume_id == "data_analyst"
    assert opened == [] and rows(factory) == []


def test_execute_does_nothing_unless_enabled(tmp_path):
    cfg, factory = make_settings(tmp_path, enabled=False), in_memory_factory()
    r, opened = go(cfg, factory, FakeClient())
    assert r.outcome == "disabled" and opened == [] and rows(factory) == []


def test_the_pause_file_stops_it(tmp_path):
    cfg, factory = make_settings(tmp_path), in_memory_factory()
    cfg.profile_refresh_pause_file.write_text("x")
    r, opened = go(cfg, factory, FakeClient())
    assert r.outcome == "paused" and opened == []


def test_a_second_run_the_same_local_day_does_nothing(tmp_path):
    cfg, factory, client = make_settings(tmp_path), in_memory_factory(), FakeClient()
    go(cfg, factory, client)
    r, opened = go(cfg, factory, client, now=MORNING + datetime.timedelta(hours=5))
    assert r.outcome == "already_done" and opened == [] and len(client.uploaded) == 1


def test_the_day_boundary_is_the_local_one_not_utc(tmp_path):
    """23:30 IST on 8 Oct is a new day by 10:00 IST on 9 Oct; 01:30 IST on 9 Oct is the same day."""
    cfg, factory, client = make_settings(tmp_path), in_memory_factory(), FakeClient()
    go(cfg, factory, client, now=datetime.datetime(2026, 10, 8, 18, 0, tzinfo=UTC))  # 23:30 IST, 8 Oct
    r, _ = go(cfg, factory, client, now=MORNING)  # 10:00 IST, 9 Oct
    assert r.outcome == "uploaded"
    cfg2, factory2, client2 = make_settings(tmp_path), in_memory_factory(), FakeClient()
    go(cfg2, factory2, client2, now=datetime.datetime(2026, 10, 8, 20, 0, tzinfo=UTC))  # 01:30 IST, 9 Oct
    r2, _ = go(cfg2, factory2, client2, now=MORNING)  # 10:00 IST, 9 Oct
    assert r2.outcome == "already_done"


def test_a_missing_resume_file_fails_without_opening_the_browser(tmp_path):
    cfg, factory = make_settings(tmp_path, files=False), in_memory_factory()
    r, opened = go(cfg, factory, FakeClient())
    assert r.outcome == "failed" and "not found" in r.detail and opened == []


# --- recording -------------------------------------------------------------------------------------------------


def test_success_is_recorded_with_a_file_hash_and_reported(tmp_path):
    cfg, factory, notes = make_settings(tmp_path), in_memory_factory(), []
    r, _ = go(cfg, factory, FakeClient(), notes=notes)
    assert r.outcome == "uploaded" and r.after_filename == "data_analyst.pdf"
    assert rows(factory) == [("data_analyst", "uploaded")]
    with session_scope(factory) as s:
        assert len(s.query(ResumeRefresh).one().file_hash) == 64
    assert notes and "uploaded data_analyst" in notes[0]


def test_an_unconfirmed_upload_does_not_advance_the_rotation(tmp_path):
    cfg, factory = make_settings(tmp_path), in_memory_factory()
    bad = FakeClient(ResumeUploadResult(after_filename="something_else.pdf", verified=False, note="not shown"))
    r, _ = go(cfg, factory, bad)
    assert r.outcome == "unconfirmed" and rows(factory) == [("data_analyst", "unconfirmed")]
    again, _ = go(cfg, factory, FakeClient(), now=MORNING + datetime.timedelta(hours=1))  # same day, retry allowed
    assert again.outcome == "uploaded" and again.resume_id == "data_analyst"  # same resume, not the next one


def test_a_login_captcha_is_recorded_as_needs_human_and_never_worked_around(tmp_path):
    cfg, factory, notes = make_settings(tmp_path), in_memory_factory(), []

    @contextmanager
    def captcha(_s):
        raise NaukriCaptchaError("captcha at https://x?token=SECRET")
        yield  # pragma: no cover

    r = run_profile_refresh(cfg, execute=True, session_factory=factory, open_client=captcha, now=MORNING, notify=notes.append)
    assert r.outcome == "needs_human" and rows(factory) == [("data_analyst", "needs_human")]
    assert "SECRET" not in r.detail and "SECRET" not in " ".join(notes)


def test_any_other_error_is_recorded_by_type_only(tmp_path):
    cfg, factory = make_settings(tmp_path), in_memory_factory()
    r, _ = go(cfg, factory, FakeClient(error=RuntimeError("boom with password=hunter2")))
    assert r.outcome == "failed" and r.detail == "RuntimeError"
    assert rows(factory) == [("data_analyst", "failed")]
    with session_scope(factory) as s:
        assert "hunter2" not in (s.query(ResumeRefresh).one().detail or "")


# --- the upload on the page ----------------------------------------------------------------------------------------


class FakePage:
    def __init__(self, names):
        self.names = list(names)  # filename shown on successive reads
        self.calls = []
        self.url = ""

    def goto(self, url, **_):
        self.calls.append(("goto", url))

    def wait_for_load_state(self, *_a, **_k):
        pass

    def wait_for_selector(self, selector, **_k):
        self.calls.append(("wait_for_selector", selector))

    def wait_for_timeout(self, _ms):
        pass

    def set_input_files(self, selector, path):
        self.calls.append(("set_input_files", selector, path))

    def reload(self):
        self.calls.append(("reload",))

    def query_selector(self, selector):
        text = {selectors.RESUME_FILENAME: self.names[0] if len(self.calls) < 4 else self.names[-1],
                selectors.RESUME_LAST_UPDATED: "Updated today"}.get(selector)
        return SimpleNamespace(inner_text=lambda: text) if text else None


def test_the_upload_sets_only_the_resume_input_and_checks_the_result_after_a_reload(tmp_path):
    f = tmp_path / "data_scientist.pdf"
    f.write_bytes(b"x")
    page = FakePage(["old_resume.pdf", "Data_Scientist.pdf"])
    res = upload_resume(page, f)
    kinds = [c[0] for c in page.calls]
    assert ("set_input_files", selectors.RESUME_UPLOAD_BUTTON, str(f)) in page.calls
    assert kinds.count("set_input_files") == 1 and kinds.index("reload") > kinds.index("set_input_files")
    assert set(kinds) <= {"goto", "wait_for_selector", "set_input_files", "reload"}  # no clicks, fills or typing
    assert res.verified is True and res.before_filename == "old_resume.pdf"


def test_an_upload_that_the_profile_does_not_show_is_not_verified(tmp_path):
    f = tmp_path / "data_scientist.pdf"
    f.write_bytes(b"x")
    res = upload_resume(FakePage(["old_resume.pdf", "old_resume.pdf"]), f)
    assert res.verified is False and "did not show" in res.note


# --- CLI ----------------------------------------------------------------------------------------------------------------


def test_cli_is_a_dry_run_unless_execute_is_given(monkeypatch):
    seen = []
    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        "naukri_agent.orchestration.profile_refresh.run_profile_refresh",
        lambda settings, execute=False, notify=None: (seen.append(execute),
                                                       SimpleNamespace(outcome="would_upload", model_dump_json=lambda indent=2: "{}"))[1],
    )
    runner = CliRunner()
    assert runner.invoke(cli_main.cli, ["profile-refresh"]).exit_code == 0
    assert runner.invoke(cli_main.cli, ["profile-refresh", "--execute"]).exit_code == 0
    assert seen == [False, True]


def test_cli_exits_nonzero_when_the_refresh_did_not_succeed(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        "naukri_agent.orchestration.profile_refresh.run_profile_refresh",
        lambda settings, execute=False, notify=None: SimpleNamespace(outcome="needs_human", model_dump_json=lambda indent=2: "{}"),
    )
    assert CliRunner().invoke(cli_main.cli, ["profile-refresh", "--execute"]).exit_code == 1
