"""Each application goes out with the resume chosen for its role: the right file is put on the
Naukri profile (Naukri applies with the profile's current resume) just before applying, only after
a Yes, and never applies if it cannot be confirmed."""

from __future__ import annotations

from types import SimpleNamespace

from naukri_agent.browser import selectors
from naukri_agent.browser.models import ResumeUploadResult
from naukri_agent.browser.profile import ensure_resume
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import ApplicationHistory, Candidate, Job
from naukri_agent.database.repositories import upsert_resume_selection
from naukri_agent.resume.selector import (
    ResumeMatchVia,
    ResumeSelectionDecision,
    ResumeSelectionOutcome,
)

from .test_auto_apply_interactive import FakeHuman, go
from .test_auto_apply_runner import FakeClient, _patch_profile, cfg, seed  # noqa: F401 - autouse fixture
from .test_profile_refresh import FakePage
from .test_telegram import JOB
from naukri_agent.orchestration.telegram_interaction import format_job_card


class ResumeClient(FakeClient):
    def __init__(self, pages, verified=True):
        super().__init__(pages)
        self.ensured = []
        self.events = []
        self._verified = verified

    def open_job_page(self, url):
        super().open_job_page(url)
        self.events.append("open")

    def ensure_profile_resume(self, path):
        self.ensured.append(path)
        self.events.append("ensure")
        return ResumeUploadResult(verified=self._verified, changed=True, note=None if self._verified else "not shown")

    def click_apply(self):
        self.events.append("click")
        super().click_apply()


def choose(factory, url, resume_id):
    """Record the role-matched resume for the job at `url` (what the daily pipeline stores)."""
    with session_scope(factory) as s:
        job = s.query(Job).filter_by(url=url).one()
        cid = s.query(Candidate).first().id
        upsert_resume_selection(
            s, job.id,
            ResumeSelectionOutcome(
                decision=ResumeSelectionDecision.SELECTED, resume_id=resume_id, file=f"resumes/{resume_id}.pdf",
                file_hash=f"hash_{resume_id}", matched_via=ResumeMatchVia.DETERMINISTIC, reason="role",
            ),
            candidate_id=cid,
        )


def test_the_roles_resume_is_put_on_the_profile_after_the_yes_and_before_applying(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("ds", "040926007001", 90.0, {})])
    choose(factory, urls["ds"], "data_scientist")
    fake = ResumeClient({urls["ds"]: {}})
    human = FakeHuman(approvals=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1
    assert [p.replace("\\", "/") for p in fake.ensured] == ["resumes/data_scientist.pdf"]
    # open page -> (Yes) -> ensure resume -> reopen the job page -> click apply
    assert fake.events == ["open", "ensure", "open", "click"]
    with session_scope(factory) as s:
        h = s.query(ApplicationHistory).one()
        assert h.resume_id == "data_scientist" and h.resume_file_hash == "hash_data_scientist"
        assert "data_scientist was put on the profile" in (h.notes or "")
    assert any("Resume: data_scientist" in n for n in human.notes if n.startswith("Applied"))


def test_a_no_never_touches_the_profile(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("ds", "040926007002", 90.0, {})])
    choose(factory, urls["ds"], "data_scientist")
    fake = ResumeClient({urls["ds"]: {}})
    r = go(c, factory, fake, FakeHuman(approvals=[False]))
    assert r.outcomes[0].outcome == "declined" and fake.ensured == [] and fake.clicked == []


def test_the_same_resume_is_only_checked_once_per_run_and_a_different_one_switches(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("a", "040926007101", 95.0, {}), ("b", "040926007102", 90.0, {}), ("c", "040926007103", 85.0, {})])
    choose(factory, urls["a"], "data_scientist")
    choose(factory, urls["b"], "data_scientist")
    choose(factory, urls["c"], "ai_ml_engineer")
    fake = ResumeClient({u: {} for u in urls.values()})
    r = go(c, factory, fake, FakeHuman(approvals=[True, True, True]))
    assert r.applied == 3
    assert [p.split("/")[-1] for p in fake.ensured] == ["data_scientist.pdf", "ai_ml_engineer.pdf"]


def test_if_the_resume_cannot_be_confirmed_nothing_is_applied_and_the_run_stops(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("a", "040926007201", 95.0, {}), ("b", "040926007202", 90.0, {})])
    choose(factory, urls["a"], "data_scientist")
    fake = ResumeClient({u: {} for u in urls.values()}, verified=False)
    r = go(c, factory, fake, FakeHuman(approvals=[True, True]))
    assert r.applied == 0 and r.outcomes[0].outcome == "failed"
    assert "nothing was applied" in r.outcomes[0].detail
    assert fake.clicked == [] and fake.submitted_urls == []
    assert "set the resume" in (r.stopped_reason or "")


def test_a_job_with_no_role_match_uses_what_is_already_on_the_profile_and_says_so(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("x", "040926007301", 90.0, {})])
    fake = ResumeClient({urls["x"]: {}})
    human = FakeHuman(approvals=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 1 and fake.ensured == []
    assert any("no role match" in n for n in human.notes if n.startswith("Applied"))
    with session_scope(factory) as s:
        h = s.query(ApplicationHistory).one()
        assert h.resume_id is None and "No resume matched" in (h.notes or "")


def test_the_telegram_card_names_the_resume():
    assert "Resume: data_scientist" in format_job_card({**JOB, "resume_id": "data_scientist"})
    assert "none matched" in format_job_card({**JOB, "resume_id": None})


# --- the page-level check ----------------------------------------------------------------------------------------


def test_ensure_resume_changes_nothing_when_the_right_file_is_already_there(tmp_path):
    f = tmp_path / "data_scientist.pdf"
    f.write_bytes(b"x")
    page = FakePage(["data_scientist.pdf", "data_scientist.pdf"])
    res = ensure_resume(page, f)
    assert res.verified and res.changed is False
    assert not any(c[0] == "set_input_files" for c in page.calls)


def test_ensure_resume_uploads_when_a_different_file_is_on_the_profile(tmp_path):
    f = tmp_path / "data_scientist.pdf"
    f.write_bytes(b"x")
    page = FakePage(["data_analyst.pdf", "data_scientist.pdf"])
    res = ensure_resume(page, f)
    assert [c[0] for c in page.calls].count("set_input_files") == 1
    assert res.verified is True
