"""The profile refresh is the FIRST step of the daily run, and can never stop the job search."""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from naukri_agent.orchestration.pipeline import _refresh_note, run_daily_recommendations

from .digest_fakes import in_memory_factory, settings
from .test_daily_pipeline import _VALID_EXTRACTION, _fake_discover, _PerJobLLM, _prep

NOW = datetime.datetime(2026, 9, 9, tzinfo=datetime.UTC)


def run(tmp_path, refresh_fn, order=None):
    _prep(tmp_path)
    cfg = settings(tmp_path, dry_run=True, threshold_review=0, threshold_accept=100)
    discover = _fake_discover([("ds", "040926000001", 0)])

    def discover_fn(*a, **k):
        if order is not None:
            order.append("discover")
        return discover(*a, **k)

    return run_daily_recommendations(
        cfg, now=NOW, discover_fn=discover_fn, extraction_provider=_PerJobLLM({}, _VALID_EXTRACTION),
        session_factory=in_memory_factory(), profile_refresh_fn=refresh_fn,
    )


def ok(outcome="uploaded", **kw):
    return SimpleNamespace(outcome=outcome, resume_id="data_scientist", detail="", after_filename="data_scientist.pdf", **kw)


def test_the_refresh_runs_before_discovery_and_is_reported_in_the_email(tmp_path):
    order = []

    def refresh(settings_, factory, now):
        order.append("refresh")
        assert now == NOW and factory is not None
        return ok()

    result = run(tmp_path, refresh, order)
    assert order == ["refresh", "discover"]
    assert result.status == "COMPLETED"
    body = Path(result.email_path).read_text(encoding="utf-8")
    assert "Profile refreshed: uploaded data_scientist (Naukri shows data_scientist.pdf)." in body
    assert body.index("Profile refreshed") < body.index("PART 1")


def test_a_crashing_refresh_never_stops_the_job_search(tmp_path):
    def refresh(*_a):
        raise RuntimeError("boom with password=hunter2")

    result = run(tmp_path, refresh)
    assert result.status == "COMPLETED" and result.recommendations >= 0
    body = Path(result.email_path).read_text(encoding="utf-8")
    assert "Profile refresh failed (RuntimeError)" in body and "hunter2" not in body


def test_when_the_feature_is_off_the_email_has_no_refresh_line_and_no_browser_is_opened(tmp_path):
    result = run(tmp_path, None)  # real default path with PROFILE_REFRESH_ENABLED unset -> "disabled"
    assert result.status == "COMPLETED"
    assert "Profile refresh" not in Path(result.email_path).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "outcome, expected",
    [
        ("uploaded", "Profile refreshed: uploaded data_scientist"),
        ("already_done", "already refreshed today"),
        ("unconfirmed", "did not show it afterwards"),
        ("needs_human", "CAPTCHA/OTP. Nothing was changed"),
        ("failed", "Profile refresh failed"),
        ("paused", "paused"),
        ("disabled", None),
        ("nothing_to_do", None),
    ],
)
def test_refresh_note_wording(outcome, expected):
    note = _refresh_note(ok(outcome))
    assert (note is None) if expected is None else (expected in note)
