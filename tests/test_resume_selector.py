import json

import yaml

from naukri_agent.database.models import Job, JobExtraction
from naukri_agent.resume.registry import ResumeMatchVia, ResumeSelectionDecision, load_resume_registry
from naukri_agent.resume.selector import classify_role_with_llm, match_deterministic, select_resume

from .fakes import FailingProvider, FakeProvider
from naukri_agent.llm.exceptions import LLMRequestError


def _registry(tmp_path, entries):
    """entries: list of (id, roles, has_file)"""
    resumes = []
    for entry_id, roles, has_file in entries:
        file_path = tmp_path / f"{entry_id}.pdf"
        if has_file:
            file_path.write_bytes(b"dummy")
        resumes.append({"id": entry_id, "file": str(file_path), "roles": roles})
    path = tmp_path / "resumes.yaml"
    path.write_text(yaml.dump({"resumes": resumes}))
    return load_resume_registry(path)


def _job(title: str) -> Job:
    return Job(
        url="https://example.com/1", title=title, company="Acme", location="Pune",
        description="d", content_fingerprint="fp",
    )


# --- Deterministic matching ---


def test_exact_role_match_selects_correct_resume(tmp_path):
    registry = _registry(
        tmp_path,
        [
            ("data_analyst", ["Data Analyst", "Business Analyst"], True),
            ("data_scientist", ["Data Scientist", "Applied Data Scientist"], True),
            ("ai_ml_engineer", ["Machine Learning Engineer", "AI Engineer", "ML Engineer"], True),
        ],
    )
    outcome = select_resume(_job("Data Scientist"), None, registry)
    assert outcome.decision == ResumeSelectionDecision.SELECTED
    assert outcome.resume_id == "data_scientist"
    assert outcome.matched_via == ResumeMatchVia.DETERMINISTIC


def test_ml_engineer_job_selects_ai_ml_resume(tmp_path):
    registry = _registry(
        tmp_path,
        [
            ("data_scientist", ["Data Scientist"], True),
            ("ai_ml_engineer", ["Machine Learning Engineer", "AI Engineer", "ML Engineer", "Computer Vision Engineer"], True),
        ],
    )
    outcome = select_resume(_job("Machine Learning Engineer"), None, registry)
    assert outcome.resume_id == "ai_ml_engineer"


def test_data_analyst_job_selects_data_analyst_resume(tmp_path):
    registry = _registry(
        tmp_path,
        [
            ("data_analyst", ["Data Analyst", "Business Analyst"], True),
            ("data_scientist", ["Data Scientist"], True),
        ],
    )
    outcome = select_resume(_job("Data Analyst"), None, registry)
    assert outcome.resume_id == "data_analyst"


def test_uses_normalized_title_from_extraction_over_raw_job_title(tmp_path):
    registry = _registry(tmp_path, [("data_scientist", ["Data Scientist"], True)])
    extraction = JobExtraction(normalized_title="Data Scientist")
    outcome = select_resume(_job("DS - Team X"), extraction, registry)
    assert outcome.resume_id == "data_scientist"


def test_match_deterministic_substring_match(tmp_path):
    registry = _registry(tmp_path, [("data_scientist", ["Data Scientist"], True)])
    matches = match_deterministic("Senior Data Scientist", registry)
    assert matches == ["data_scientist"]


# --- Ambiguity -> REVIEW ---


def test_no_matching_role_gives_review(tmp_path):
    registry = _registry(tmp_path, [("data_scientist", ["Data Scientist"], True)])
    outcome = select_resume(_job("Sales Executive"), None, registry)
    assert outcome.decision == ResumeSelectionDecision.REVIEW
    assert outcome.resume_id is None


def test_multiple_matching_roles_gives_review(tmp_path):
    registry = _registry(
        tmp_path,
        [
            ("data_scientist", ["Data Scientist", "Analyst"], True),
            ("data_analyst", ["Data Analyst", "Analyst"], True),
        ],
    )
    outcome = select_resume(_job("Analyst"), None, registry)
    assert outcome.decision == ResumeSelectionDecision.REVIEW


def test_empty_registry_gives_review(tmp_path):
    registry = _registry(tmp_path, [])
    outcome = select_resume(_job("Data Scientist"), None, registry)
    assert outcome.decision == ResumeSelectionDecision.REVIEW


# --- Missing file forces REVIEW regardless of match confidence ---


def test_matched_resume_with_missing_file_gives_review(tmp_path):
    registry = _registry(tmp_path, [("data_scientist", ["Data Scientist"], False)])  # no file
    outcome = select_resume(_job("Data Scientist"), None, registry)
    assert outcome.decision == ResumeSelectionDecision.REVIEW
    assert "not found" in outcome.reason.lower() or "missing" in outcome.reason.lower() or "file" in outcome.reason.lower()


def test_selected_outcome_includes_file_hash(tmp_path):
    registry = _registry(tmp_path, [("data_scientist", ["Data Scientist"], True)])
    outcome = select_resume(_job("Data Scientist"), None, registry)
    assert outcome.file_hash is not None
    assert outcome.file is not None


# --- LLM-assisted classification ---


def test_llm_resolves_ambiguous_match(tmp_path):
    registry = _registry(
        tmp_path,
        [
            ("data_scientist", ["Data Scientist", "Analyst"], True),
            ("data_analyst", ["Data Analyst", "Analyst"], True),
        ],
    )
    provider = FakeProvider(model="m", response=json.dumps({"resume_id": "data_scientist"}))
    outcome = select_resume(_job("Analyst"), None, registry, provider=provider)
    assert outcome.decision == ResumeSelectionDecision.SELECTED
    assert outcome.resume_id == "data_scientist"
    assert outcome.matched_via == ResumeMatchVia.LLM


def test_llm_deterministic_match_not_consulted_when_unambiguous(tmp_path):
    """LLM should not even be asked when deterministic matching already resolved cleanly."""
    registry = _registry(tmp_path, [("data_scientist", ["Data Scientist"], True)])
    provider = FakeProvider(model="m", response=json.dumps({"resume_id": "data_scientist"}))
    select_resume(_job("Data Scientist"), None, registry, provider=provider)
    assert provider.last_prompt is None  # never called


def test_llm_returning_unknown_id_is_ignored_and_falls_back_to_review(tmp_path):
    registry = _registry(
        tmp_path,
        [
            ("data_scientist", ["Data Scientist", "Analyst"], True),
            ("data_analyst", ["Data Analyst", "Analyst"], True),
        ],
    )
    provider = FakeProvider(model="m", response=json.dumps({"resume_id": "not_a_real_id"}))
    outcome = select_resume(_job("Analyst"), None, registry, provider=provider)
    assert outcome.decision == ResumeSelectionDecision.REVIEW


def test_llm_returning_null_falls_back_to_review(tmp_path):
    registry = _registry(
        tmp_path,
        [
            ("data_scientist", ["Data Scientist", "Analyst"], True),
            ("data_analyst", ["Data Analyst", "Analyst"], True),
        ],
    )
    provider = FakeProvider(model="m", response=json.dumps({"resume_id": None}))
    outcome = select_resume(_job("Analyst"), None, registry, provider=provider)
    assert outcome.decision == ResumeSelectionDecision.REVIEW


def test_llm_failure_falls_back_to_review_without_raising(tmp_path):
    registry = _registry(
        tmp_path,
        [
            ("data_scientist", ["Data Scientist", "Analyst"], True),
            ("data_analyst", ["Data Analyst", "Analyst"], True),
        ],
    )
    provider = FailingProvider(model="m", error=LLMRequestError("boom"))
    outcome = select_resume(_job("Analyst"), None, registry, provider=provider)
    assert outcome.decision == ResumeSelectionDecision.REVIEW


def test_classify_role_with_llm_never_returns_id_outside_registry(tmp_path):
    registry = _registry(tmp_path, [("data_scientist", ["Data Scientist"], True)])
    provider = FakeProvider(model="m", response=json.dumps({"resume_id": "made_up_category"}))
    result = classify_role_with_llm(provider, "Data Scientist", None, registry)
    assert result is None


def test_no_provider_given_skips_llm_and_goes_straight_to_review(tmp_path):
    registry = _registry(
        tmp_path,
        [
            ("data_scientist", ["Data Scientist", "Analyst"], True),
            ("data_analyst", ["Data Analyst", "Analyst"], True),
        ],
    )
    outcome = select_resume(_job("Analyst"), None, registry, provider=None)
    assert outcome.decision == ResumeSelectionDecision.REVIEW
