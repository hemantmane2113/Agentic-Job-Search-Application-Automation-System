import pytest

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import Job, JobExtraction
from naukri_agent.matching.experience_matcher import EXPERIENCE_SLIGHT_SHORTFALL_GRACE_YEARS
from naukri_agent.matching.models import MatchDecision
from naukri_agent.matching.scorer import score_job
from naukri_agent.resume.models import MasterResume


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _job(**overrides) -> Job:
    defaults = dict(
        url="https://example.com/1",
        title="Data Scientist",
        company="Acme",
        location="Pune",
        description="d",
        content_fingerprint="fp",
    )
    defaults.update(overrides)
    return Job(**defaults)


def _strong_profile() -> CandidateProfile:
    return CandidateProfile(
        full_name="X",
        email="x@example.com",
        phone="1",
        skills=["Python", "SQL", "Machine Learning"],
        years_experience=3,
        preferred_roles=["Data Scientist"],
        preferred_locations=["Pune"],
        expected_salary_min_lpa=8,
    )


def _weak_profile() -> CandidateProfile:
    return CandidateProfile(
        full_name="X",
        email="x@example.com",
        phone="1",
        skills=["Excel"],
        years_experience=0,
        preferred_roles=["Sales Executive"],
        preferred_locations=["Mumbai"],
        expected_salary_min_lpa=20,
    )


def _resume() -> MasterResume:
    return MasterResume(professional_summary="s")


def _strong_extraction() -> JobExtraction:
    return JobExtraction(
        normalized_title="Data Scientist",
        required_skills=["Python", "SQL"],
        preferred_skills=["Machine Learning"],
        experience_min=2,
        experience_max=5,
        salary_max=14,
    )


def test_strong_match_produces_accept_decision():
    settings = _settings()
    result = score_job(_job(), _strong_extraction(), _strong_profile(), _resume(), settings)
    assert result.decision == MatchDecision.ACCEPT
    assert result.overall_score >= settings.threshold_accept


def test_weak_match_produces_reject_decision():
    settings = _settings()
    result = score_job(
        _job(location="Mumbai"), _strong_extraction(), _weak_profile(), _resume(), settings
    )
    assert result.decision == MatchDecision.REJECT
    assert result.overall_score < settings.threshold_review


def test_borderline_match_produces_review_decision():
    settings = _settings(threshold_accept=80, threshold_review=70)
    # Meets skills/role/location but salary unknown and experience unspecified
    extraction = JobExtraction(
        normalized_title="Data Scientist",
        required_skills=["Python", "SQL"],
        preferred_skills=["Machine Learning"],
    )
    result = score_job(_job(), extraction, _strong_profile(), _resume(), settings)
    assert result.decision in (MatchDecision.REVIEW, MatchDecision.ACCEPT)
    # explicitly check it's not a silent full-credit outcome
    assert any("incomplete" in f.lower() or "not specified" in f.lower() for f in result.negative_factors)


def test_overall_score_is_weighted_sum_normalized_to_100():
    settings = _settings()
    result = score_job(_job(), _strong_extraction(), _strong_profile(), _resume(), settings)
    total_max = sum(cs.max_points for cs in result.category_scores.values())
    total_points = sum(cs.points for cs in result.category_scores.values())
    expected = round((total_points / total_max) * 100, 1)
    assert result.overall_score == expected


def test_all_six_categories_present_in_result():
    settings = _settings()
    result = score_job(_job(), _strong_extraction(), _strong_profile(), _resume(), settings)
    assert set(result.category_scores.keys()) == {
        "skills",
        "experience",
        "role",
        "salary",
        "location",
        "education",
    }


def test_positive_and_negative_factors_are_aggregated_from_categories():
    settings = _settings()
    result = score_job(_job(), _strong_extraction(), _strong_profile(), _resume(), settings)
    assert len(result.positive_factors) > 0


def test_missing_extraction_does_not_crash_and_degrades_gracefully():
    settings = _settings()
    result = score_job(_job(), None, _strong_profile(), _resume(), settings)
    assert 0 <= result.overall_score <= 100


def test_weights_do_not_need_to_sum_to_100():
    settings = _settings(
        weight_skills=10, weight_experience=10, weight_role=10,
        weight_salary=10, weight_location=10, weight_education=10,
    )
    result = score_job(_job(), _strong_extraction(), _strong_profile(), _resume(), settings)
    assert 0 <= result.overall_score <= 100


# --- Run 16 fix A: explicit minimum-experience violation = hard REJECT ---
#
# _strong_profile() has years_experience=3 and otherwise-strong
# skills/role/salary/location/education, so these cases isolate the new
# override: without it, an explicit 4-9 or 5-10 minimum would still
# score high enough (via the other five categories) to ACCEPT/REVIEW --
# proving the fix is what changes the decision, not incidentally weak
# scoring elsewhere.


def _extraction_with_experience(exp_min, exp_max) -> JobExtraction:
    return JobExtraction(
        normalized_title="Data Scientist",
        required_skills=["Python", "SQL"],
        preferred_skills=["Machine Learning"],
        experience_min=exp_min,
        experience_max=exp_max,
        salary_max=14,
    )


@pytest.mark.parametrize(
    "exp_min,exp_max,expect_reject",
    [
        (3, 5, False),      # 3 yrs candidate vs 3-5 -> eligible
        (3, 8, False),      # 3 yrs candidate vs 3-8 -> eligible
        (4, 9, True),       # 3 yrs candidate vs 4-9 -> REJECT
        (5, 10, True),      # 3 yrs candidate vs 5-10 -> REJECT
        (0, 5, False),      # 3 yrs candidate vs 0-5 -> eligible
        (2, 4, False),      # 3 yrs candidate vs 2-4 -> eligible
        (3, None, False),   # 3 yrs candidate vs "3+" (open-ended) -> eligible
    ],
)
def test_hard_minimum_experience_eligibility_matrix(exp_min, exp_max, expect_reject):
    settings = _settings()
    extraction = _extraction_with_experience(exp_min, exp_max)
    result = score_job(_job(), extraction, _strong_profile(), _resume(), settings)
    if expect_reject:
        assert result.decision == MatchDecision.REJECT
    else:
        assert result.decision != MatchDecision.REJECT


def test_score_alone_would_have_accepted_the_4_to_9_case_without_the_override():
    """Proves the override -- not a naturally low score -- drives the
    REJECT: category math alone clears threshold_accept."""
    settings = _settings()
    extraction = _extraction_with_experience(4, 9)
    result = score_job(_job(), extraction, _strong_profile(), _resume(), settings)
    total_max = sum(cs.max_points for cs in result.category_scores.values())
    total_points = sum(cs.points for cs in result.category_scores.values())
    assert result.overall_score == round((total_points / total_max) * 100, 1)
    assert result.overall_score >= settings.threshold_accept  # score itself is strong
    assert result.decision == MatchDecision.REJECT             # but hard-excluded anyway


def test_hard_reject_does_not_alter_category_points_or_overall_score():
    """Requirement: do not change experience weight, category points,
    overall score, or thresholds -- only `decision` may change."""
    settings = _settings()
    extraction = _extraction_with_experience(4, 9)
    result = score_job(_job(), extraction, _strong_profile(), _resume(), settings)
    assert result.category_scores["experience"].max_points == settings.weight_experience
    assert result.category_scores["experience"].points == settings.weight_experience * 0.25


def test_hard_rejected_job_still_carries_the_explanatory_negative_factor():
    settings = _settings()
    extraction = _extraction_with_experience(4, 9)
    result = score_job(_job(), extraction, _strong_profile(), _resume(), settings)
    assert result.decision == MatchDecision.REJECT
    assert any("below minimum" in f.lower() for f in result.negative_factors)


def test_missing_minimum_experience_is_not_rejected_by_this_rule():
    settings = _settings()
    extraction = JobExtraction(
        normalized_title="Data Scientist",
        required_skills=["Python", "SQL"],
        preferred_skills=["Machine Learning"],
        experience_min=None,
        experience_max=None,
        salary_max=14,
    )
    result = score_job(_job(), extraction, _strong_profile(), _resume(), settings)
    # unknown-credit path applies (unchanged); the hard rule never fires
    # for an unstated minimum, so this stays a strong ACCEPT.
    assert result.decision == MatchDecision.ACCEPT


def test_none_extraction_is_not_rejected_by_this_rule():
    settings = _settings()
    result = score_job(_job(), None, _strong_profile(), _resume(), settings)
    assert result.decision != MatchDecision.REJECT


def test_shortfall_within_the_grace_window_is_not_hard_rejected():
    """A candidate 0.5 yrs (the shared grace constant) below the stated
    minimum stays on the existing partial-credit path, not the hard
    override -- consistent with score_experience's own boundary."""
    settings = _settings()
    extraction = _extraction_with_experience(3 + EXPERIENCE_SLIGHT_SHORTFALL_GRACE_YEARS, 8)
    result = score_job(_job(), extraction, _strong_profile(), _resume(), settings)
    assert result.decision != MatchDecision.REJECT


# --- Run 17 follow-up: conflicting experience requirements -----------------
#
# score_job derives the conflict classification FRESH from
# job.experience_text/job.description (classify_experience_requirements,
# jobs/parser.py) -- independent of whatever extraction.experience_min/max
# holds. _job() below is called WITH experience_text/description overrides
# to exercise this; every PRE-EXISTING test above calls plain _job() (no
# experience_text, description="d") which resolves to "unknown" and falls
# back to trusting extraction.experience_min exactly as before this phase
# -- which is exactly why none of them needed to change.


def test_I_conflict_forces_review_even_when_score_would_otherwise_accept():
    """Example I: skills/role/salary/location/education alone already
    clear threshold_accept, but a genuine experience conflict caps the
    decision at REVIEW -- never silently ACCEPT."""
    settings = _settings()
    job = _job(
        experience_text="4 - 5 years",
        description="Data Scientist - Kandivali\nExp - 3+\nBudget - 10",
    )
    extraction = _extraction_with_experience(3, 5)  # the LLM's raw, unreconciled blend
    result = score_job(job, extraction, _strong_profile(), _resume(), settings)

    non_experience_points = sum(
        cs.points for name, cs in result.category_scores.items() if name != "experience"
    )
    non_experience_max = sum(
        cs.max_points for name, cs in result.category_scores.items() if name != "experience"
    )
    assert non_experience_points / non_experience_max >= 0.9  # genuinely strong elsewhere
    assert result.decision == MatchDecision.REVIEW
    assert any("Experience conflict" in f for f in result.negative_factors)


def test_talent_corner_reproduction_through_score_job_is_review():
    settings = _settings()
    job = _job(
        experience_text="4 - 5 years",
        description="Data Scientist - Kandivali\nExp - 3+\nBudget - 10",
    )
    extraction = _extraction_with_experience(3, 5)
    result = score_job(job, extraction, _strong_profile(), _resume(), settings)
    assert result.decision == MatchDecision.REVIEW
    assert any("4-5" in f and "3+" in f for f in result.negative_factors)


def test_praxis_reproduction_through_score_job_is_review():
    settings = _settings()
    job = _job(
        experience_text="2 - 4 years",
        description=(
            "Location: Mumbai, Work From Office\n"
            "Experience Level: 2-4+ Years\n"
            "Required Skills & Qualifications Bachelor's degree\n"
            "5+ years of experience in machine learning engineering\n"
        ),
    )
    extraction = _extraction_with_experience(5, None)
    result = score_job(job, extraction, _strong_profile(), _resume(), settings)
    assert result.decision == MatchDecision.REVIEW
    assert any("2-4" in f and "5+" in f for f in result.negative_factors)


def test_el_shaddai_reproduction_through_score_job_is_reject_with_no_conflict_message():
    settings = _settings()
    job = _job(
        experience_text="8 - 13 years",
        description="Data Scientist with strong hands-on experience in Python, AI, ML.",
    )
    extraction = _extraction_with_experience(8, 13)
    result = score_job(job, extraction, _strong_profile(), _resume(), settings)
    assert result.decision == MatchDecision.REJECT
    assert not any("conflict" in f.lower() for f in result.negative_factors)
    assert any("below minimum" in f.lower() for f in result.negative_factors)


def test_exclude_multi_through_score_job_rejects_with_multi_source_message():
    """Two genuinely different minimums (5-8 structured, 9+ required in
    the body), both exclude the candidate -> REJECT, not REVIEW, but the
    explanation names both sources."""
    settings = _settings()
    job = _job(
        experience_text="5 - 8 years",
        description="Requirements: minimum 9 years of experience.",
    )
    extraction = _extraction_with_experience(9, None)
    result = score_job(job, extraction, _strong_profile(), _resume(), settings)
    assert result.decision == MatchDecision.REJECT
    assert any("5-8" in f and "9+" in f for f in result.negative_factors)


def test_unknown_state_falls_back_to_pre_existing_part_a_behavior():
    """No deterministic candidate from job text at all (plain _job(), no
    experience_text, description="d") -> falls back to trusting
    extraction.experience_min exactly as before this phase."""
    settings = _settings()
    extraction = _extraction_with_experience(4, 9)
    result = score_job(_job(), extraction, _strong_profile(), _resume(), settings)
    assert result.decision == MatchDecision.REJECT
    assert not any("conflict" in f.lower() for f in result.negative_factors)


def test_conflict_message_reaches_the_existing_negative_factors_list():
    """The conflict message rides the PRE-EXISTING MatchResult.negative_
    factors field all the way toward the digest (recommendations/
    builder.py -> explain_match -> Recommendation.gaps ->
    notifications/render.py) -- no new field, no schema change, needed
    anywhere in that chain."""
    settings = _settings()
    job = _job(
        experience_text="4 - 5 years",
        description="Data Scientist - Kandivali\nExp - 3+\nBudget - 10",
    )
    extraction = _extraction_with_experience(3, 5)
    result = score_job(job, extraction, _strong_profile(), _resume(), settings)
    assert isinstance(result.negative_factors, list)
    assert any(f.startswith("⚠️ Experience conflict") for f in result.negative_factors)


# --- Required-skill coverage gate (Run 17 skill-quality follow-up) --------
#
# End-to-end (score_job) coverage of the approved 60% required-skill
# floor: it must only ever cap ACCEPT -> REVIEW, never touch REJECT,
# never touch an already-REVIEW decision, and never gate at all when
# required_skills is empty. All other categories (experience/role/
# salary/location/education) are held at their maximum in these fixtures
# so the skill category is the only thing driving the observed decision
# change -- isolating the gate's effect cleanly.

_REQUIRED_FIVE = ["a", "b", "c", "d", "e"]


def _gate_profile(matched_required: list[str]) -> CandidateProfile:
    return CandidateProfile(
        full_name="X",
        email="x@example.com",
        phone="1",
        skills=matched_required,
        years_experience=3,
        preferred_roles=["Data Scientist"],
        preferred_locations=["Pune"],
        expected_salary_min_lpa=8,
    )


def _gate_extraction(required: list[str], preferred: list[str] | None = None) -> JobExtraction:
    return JobExtraction(
        normalized_title="Data Scientist",
        required_skills=required,
        preferred_skills=preferred or [],
        experience_min=2,
        experience_max=5,
        salary_max=14,
    )


def test_B_4_of_5_required_no_gate_accept_stays_accept():
    """4/5 = 80% -- above the 60% floor, ACCEPT must be untouched."""
    settings = _settings()
    profile = _gate_profile(_REQUIRED_FIVE[:4])
    extraction = _gate_extraction(_REQUIRED_FIVE)
    result = score_job(_job(), extraction, profile, _resume(), settings)
    assert result.overall_score >= settings.threshold_accept  # pre-gate was already ACCEPT
    assert result.decision == MatchDecision.ACCEPT
    assert not any("Required skill coverage" in f for f in result.negative_factors)


def test_D_2_of_5_required_aggregate_accept_capped_to_review():
    """2/5 = 40% -- below the floor. Pre-gate score (83.2, default
    thresholds) is genuinely ACCEPT-range; the gate must cap it."""
    settings = _settings()
    profile = _gate_profile(_REQUIRED_FIVE[:2])
    extraction = _gate_extraction(_REQUIRED_FIVE)
    result = score_job(_job(), extraction, profile, _resume(), settings)
    assert result.overall_score >= settings.threshold_accept  # would have been ACCEPT
    assert result.decision == MatchDecision.REVIEW
    assert any(
        f == "⚠️ Required skill coverage is 40% (2/5). Review required skills before applying."
        for f in result.negative_factors
    )


def test_E_1_of_5_required_aggregate_accept_capped_to_review():
    """1/5 = 20%. Under DEFAULT weights this case can never naturally
    score high enough to be pre-gate ACCEPT (max achievable total with
    every other category maxed is 77.6/100) -- threshold_accept is
    lowered here ONLY to construct a genuine pre-gate-ACCEPT precondition
    so the cap is actually exercised, not incidentally satisfied by the
    ordinary REVIEW threshold. Production weights/thresholds are
    untouched; this is a test-local Settings override."""
    settings = _settings(threshold_accept=75)
    profile = _gate_profile(_REQUIRED_FIVE[:1])
    extraction = _gate_extraction(_REQUIRED_FIVE)
    result = score_job(_job(), extraction, profile, _resume(), settings)
    assert result.overall_score >= settings.threshold_accept  # genuinely ACCEPT pre-gate
    assert result.decision == MatchDecision.REVIEW
    assert any(
        f == "⚠️ Required skill coverage is 20% (1/5). Review required skills before applying."
        for f in result.negative_factors
    )


def test_F_0_of_5_required_aggregate_accept_capped_to_review():
    """0/5 = 0%. Same rationale as test_E -- threshold_accept lowered
    test-locally only to construct a genuine pre-gate ACCEPT."""
    settings = _settings(threshold_accept=70)
    profile = _gate_profile([])
    extraction = _gate_extraction(_REQUIRED_FIVE)
    result = score_job(_job(), extraction, profile, _resume(), settings)
    assert result.overall_score >= settings.threshold_accept  # genuinely ACCEPT pre-gate
    assert result.decision == MatchDecision.REVIEW
    assert any(
        f == "⚠️ Required skill coverage is 0% (0/5). Review required skills before applying."
        for f in result.negative_factors
    )


def test_G_2_of_5_required_aggregate_review_stays_review():
    """2/5 = 40%, but the aggregate was already REVIEW (threshold_accept
    raised test-locally to 90 so the same 83.2 score lands in REVIEW
    range pre-gate) -- the gate must leave it exactly as REVIEW, never
    push it further down."""
    settings = _settings(threshold_accept=90)
    profile = _gate_profile(_REQUIRED_FIVE[:2])
    extraction = _gate_extraction(_REQUIRED_FIVE)
    result = score_job(_job(), extraction, profile, _resume(), settings)
    assert result.overall_score < settings.threshold_accept  # genuinely REVIEW pre-gate
    assert result.overall_score >= settings.threshold_review
    assert result.decision == MatchDecision.REVIEW


def test_H_2_of_5_required_aggregate_reject_stays_reject():
    """2/5 = 40%, but role/location/salary are weak enough that the
    aggregate is REJECT outright -- the gate must never raise a REJECT
    to REVIEW."""
    settings = _settings()
    profile = _gate_profile(_REQUIRED_FIVE[:2])
    extraction = JobExtraction(
        normalized_title="Something Else",
        required_skills=_REQUIRED_FIVE,
        preferred_skills=[],
        experience_min=2,
        experience_max=5,
    )
    result = score_job(_job(location="Mumbai"), extraction, profile, _resume(), settings)
    assert result.overall_score < settings.threshold_review  # genuinely REJECT
    assert result.decision == MatchDecision.REJECT
    # the informative negative_factor is still added even though the
    # decision itself is untouched
    assert any(
        f == "⚠️ Required skill coverage is 40% (2/5). Review required skills before applying."
        for f in result.negative_factors
    )


def test_I_required_empty_preferred_6_of_29_no_skill_gate_at_scorer_level():
    """required=[] -- no confirmed-mandatory signal, so no gate message
    at all, regardless of how weak the (optional) preferred coverage
    is. Reproduces the Spectrum-style shape."""
    settings = _settings()
    preferred = [f"skill{i}" for i in range(29)]
    matched = [f"skill{i}" for i in range(6)]
    profile = _gate_profile(matched)
    extraction = _gate_extraction(required=[], preferred=preferred)
    result = score_job(_job(), extraction, profile, _resume(), settings)
    assert not any("Required skill coverage" in f for f in result.negative_factors)


def test_J_required_empty_preferred_7_of_17_no_skill_gate_at_scorer_level():
    """Reproduces the Wabtec-style shape: same -- required=[] means no
    gate message, independent of the preferred ratio."""
    settings = _settings()
    preferred = [f"skill{i}" for i in range(17)]
    matched = [f"skill{i}" for i in range(7)]
    profile = _gate_profile(matched)
    extraction = _gate_extraction(required=[], preferred=preferred)
    result = score_job(_job(), extraction, profile, _resume(), settings)
    assert not any("Required skill coverage" in f for f in result.negative_factors)


def test_K_large_preferred_list_does_not_affect_the_gate_end_to_end():
    """4/5 required (80%, no gate) + a 40-item, almost entirely
    unmatched preferred list must not trigger the gate -- it never
    looks at preferred_skills, and the blended skill *points* dropping
    (because of the weak preferred ratio) must not be confused with the
    required-only gate firing."""
    settings = _settings()
    preferred = [f"pref{i}" for i in range(40)]
    profile = _gate_profile(_REQUIRED_FIVE[:4])
    extraction = _gate_extraction(_REQUIRED_FIVE, preferred=preferred)
    result = score_job(_job(), extraction, profile, _resume(), settings)
    assert not any("Required skill coverage" in f for f in result.negative_factors)


def test_L_experience_conflict_and_skill_gate_together_review_no_duplicate_messages():
    """Both deterministic overrides fire independently on the same job:
    an experience conflict (Talent Corner style: structured 4-5 vs JD
    'Exp - 3+') AND a required-skill gate (2/5 = 40%). Final decision
    must be REVIEW (both mechanisms agree), both distinct messages must
    be present, and neither message may be duplicated."""
    settings = _settings()
    job = _job(
        experience_text="4 - 5 years",
        description="Data Scientist - Kandivali\nExp - 3+\nBudget - 10",
    )
    profile = _gate_profile(_REQUIRED_FIVE[:2])
    extraction = _gate_extraction(_REQUIRED_FIVE)
    result = score_job(job, extraction, profile, _resume(), settings)

    assert result.decision == MatchDecision.REVIEW
    conflict_messages = [f for f in result.negative_factors if f.startswith("⚠️ Experience conflict")]
    gate_messages = [f for f in result.negative_factors if f.startswith("⚠️ Required skill coverage")]
    assert len(conflict_messages) == 1
    assert len(gate_messages) == 1
    assert gate_messages[0] == (
        "⚠️ Required skill coverage is 40% (2/5). Review required skills before applying."
    )


def test_M_exactly_60_percent_required_coverage_no_gate_end_to_end():
    """3/5 = exactly 60% -- must NOT trigger the gate (strict '< 60%')."""
    settings = _settings()
    profile = _gate_profile(_REQUIRED_FIVE[:3])
    extraction = _gate_extraction(_REQUIRED_FIVE)
    result = score_job(_job(), extraction, profile, _resume(), settings)
    assert not any("Required skill coverage" in f for f in result.negative_factors)


def test_N_just_below_60_percent_required_coverage_gate_triggers_end_to_end():
    """7/12 = 58.33...% -- just under the 60% floor, must trigger."""
    settings = _settings()
    required = [f"req{i}" for i in range(12)]
    matched = [f"req{i}" for i in range(7)]
    profile = _gate_profile(matched)
    extraction = _gate_extraction(required)
    result = score_job(_job(), extraction, profile, _resume(), settings)
    assert any(
        f == "⚠️ Required skill coverage is 58% (7/12). Review required skills before applying."
        for f in result.negative_factors
    )
