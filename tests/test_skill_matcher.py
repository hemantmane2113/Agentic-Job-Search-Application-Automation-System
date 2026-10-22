import pytest

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import JobExtraction
from naukri_agent.matching.models import ExperienceProfile
from naukri_agent.matching.skill_matcher import (
    format_required_skill_coverage_message,
    required_skill_coverage,
    required_skill_coverage_below_floor,
    score_skills,
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _profile(skills: list[str]) -> CandidateProfile:
    return CandidateProfile(full_name="X", email="x@example.com", phone="1", skills=skills)


def _extraction(required=None, preferred=None) -> JobExtraction:
    return JobExtraction(required_skills=required or [], preferred_skills=preferred or [])


def test_all_required_skills_matched_gives_full_credit():
    settings = _settings()
    profile = _profile(["Python", "SQL"])
    extraction = _extraction(required=["Python", "SQL"])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert result.points == result.max_points


def test_missing_required_skill_costs_more_than_missing_preferred():
    settings = _settings()
    profile = _profile(["Python"])

    missing_required = _extraction(required=["Python", "SQL"], preferred=[])
    missing_preferred = _extraction(required=["Python"], preferred=["SQL"])

    score_missing_required = score_skills(
        missing_required, profile, ExperienceProfile(total_years=3), settings
    )
    score_missing_preferred = score_skills(
        missing_preferred, profile, ExperienceProfile(total_years=3), settings
    )
    assert score_missing_required.points < score_missing_preferred.points


def test_missing_required_skill_produces_negative_factor():
    settings = _settings()
    profile = _profile(["Python"])
    extraction = _extraction(required=["Python", "AWS"])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert any("AWS" in f for f in result.negative_factors)


def test_matched_required_skill_produces_positive_factor():
    settings = _settings()
    profile = _profile(["Python"])
    extraction = _extraction(required=["Python"])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert any("Python" in f for f in result.positive_factors)


def test_skill_alias_is_recognized_as_match():
    settings = _settings()
    profile = _profile(["Machine Learning"])
    extraction = _extraction(required=["ML"])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert result.points == result.max_points


def test_no_job_skills_specified_gives_full_credit():
    settings = _settings()
    profile = _profile([])
    extraction = _extraction(required=[], preferred=[])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert result.points == result.max_points


def test_none_extraction_treated_as_no_requirements():
    settings = _settings()
    profile = _profile(["Python"])
    result = score_skills(None, profile, ExperienceProfile(total_years=3), settings)
    assert result.points == result.max_points


def test_skill_years_annotated_in_positive_factor_when_available():
    settings = _settings()
    profile = _profile(["Python"])
    extraction = _extraction(required=["Python"])
    experience = ExperienceProfile(total_years=3, skill_years={"python": 4.2})
    result = score_skills(extraction, profile, experience, settings)
    assert any("4.2" in f for f in result.positive_factors)


def test_no_skill_years_data_gives_plain_factor_not_invented():
    settings = _settings()
    profile = _profile(["Python"])
    extraction = _extraction(required=["Python"])
    experience = ExperienceProfile(total_years=3, skill_years={})
    result = score_skills(extraction, profile, experience, settings)
    assert result.positive_factors == ["Python required skill matched"]


# --- Run 17 fix: required=[] no longer grants a free 0.8x credit ----------
#
# Before the fix: required=[] -> req_ratio=1.0 -> combined = 0.8*1.0 +
# 0.2*pref_ratio, so a job could score ~88% skills coverage while only
# 7 of 17 (41%) of its actually-listed skills matched (Wabtec, Run 17).
# After the fix: required=[] and preferred non-empty -> combined_ratio
# IS pref_ratio -- the preferred list carries the full weight instead
# of being capped at 20%.


def test_wabtec_style_required_empty_preferred_7_of_17_gives_plain_coverage():
    """Reproduces Run 17 job 62 (Wabtec) exactly: required=[], 17
    preferred skills, 7 matched. Before the fix this scored 30.88/35
    (as if ~88% coverage); after the fix it must equal 35 * 7/17."""
    settings = _settings()
    preferred = [
        "SQL", "Python", "R", "Spark", "HiveSQL", "Hive", "Postgres", "MySQL",
        "SQL Server", "Hadoop", "NoSQL", "Machine Learning", "Deep Learning",
        "Generative AI", "Large Language Models (LLMs)", "Agent Development",
        "Advanced Analytics",
    ]
    assert len(preferred) == 17
    matched = ["SQL", "Python", "R", "MySQL", "Machine Learning", "Deep Learning", "Generative AI"]
    assert len(matched) == 7
    profile = _profile(matched)
    extraction = _extraction(required=[], preferred=preferred)
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)

    expected_ratio = 7 / 17
    assert result.points == pytest.approx(settings.weight_skills * expected_ratio)
    # not the old (inflated) 0.8*1.0 + 0.2*(7/17) result -- the actual
    # persisted Run 17 value before this fix
    old_formula_points = settings.weight_skills * (0.8 * 1.0 + 0.2 * expected_ratio)
    assert old_formula_points == pytest.approx(30.88235294117647)
    assert result.points < old_formula_points


def test_spectrum_style_required_empty_preferred_6_of_29_gives_plain_coverage():
    """Reproduces Run 17 job 21 (Spectrum): required=[], 29 preferred
    skills, 6 matched. Must equal 35 * 6/29, not the old inflated value."""
    settings = _settings()
    preferred = [
        "Python", "R", "KNIME", "PyTorch/TensorFlow", "Scikit-learn", "Pandas",
        "MLflow", "DVC", "Docker", "AWS", "Azure", "GCP", "Hadoop", "Spark",
        "MySQL", "Microsoft SQL Server", "MongoDB", "DynamoDB", "Power BI",
        "Tableau", "Alteryx", "MS Excel", "Business Intelligence",
        "Statistical Modelling", "Machine Learning", "Big Data Technologies",
        "Data Engineering", "Statistical Inference", "Feature Engineering",
    ]
    assert len(preferred) == 29
    matched = ["Python", "R", "Scikit-learn", "Pandas", "MySQL", "Machine Learning"]
    assert len(matched) == 6
    profile = _profile(matched)
    extraction = _extraction(required=[], preferred=preferred)
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)

    expected_ratio = 6 / 29
    assert result.points == pytest.approx(settings.weight_skills * expected_ratio)
    old_formula_points = settings.weight_skills * (0.8 * 1.0 + 0.2 * expected_ratio)
    assert result.points < old_formula_points


def test_required_and_preferred_both_populated_formula_is_unchanged():
    """The 0.8/0.2 weighted formula must be byte-identical to before
    whenever required_skills is non-empty (Run 17 job 20, Wissda:
    required 6/7 matched, preferred 0/4 matched -> 35*0.6857.. = 24.0)."""
    settings = _settings()
    required = ["Python", "SQL", "Machine Learning", "Statistical Modeling",
                "Deep Learning", "TensorFlow", "PyTorch"]
    preferred = ["MLOps", "Model Evaluation", "Validation", "Feature Engineering"]
    matched = ["Python", "SQL", "Machine Learning", "Deep Learning", "TensorFlow", "PyTorch"]
    profile = _profile(matched)
    extraction = _extraction(required=required, preferred=preferred)
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)

    req_ratio = 6 / 7
    pref_ratio = 0 / 4
    expected = settings.weight_skills * (
        settings.required_skills_weight_ratio * req_ratio
        + (1 - settings.required_skills_weight_ratio) * pref_ratio
    )
    assert result.points == pytest.approx(expected)
    assert result.points == pytest.approx(24.0)


def test_both_lists_empty_still_gives_full_credit():
    settings = _settings()
    profile = _profile([])
    extraction = _extraction(required=[], preferred=[])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert result.points == result.max_points


def test_required_empty_preferred_empty_is_distinct_from_required_empty_preferred_populated():
    """Guard: the new elif/else branching must not collapse these two
    genuinely different cases to the same credit."""
    settings = _settings()
    profile = _profile(["Python"])
    both_empty = score_skills(
        _extraction(required=[], preferred=[]), profile, ExperienceProfile(total_years=3), settings
    )
    preferred_populated_none_matched = score_skills(
        _extraction(required=[], preferred=["AWS", "Azure"]), profile,
        ExperienceProfile(total_years=3), settings,
    )
    assert both_empty.points == both_empty.max_points  # 1.0 -> full credit
    assert preferred_populated_none_matched.points == 0.0  # pref_ratio = 0/2


# --- Required-skill coverage gate (Run 17 skill-quality follow-up) --------
#
# Pure unit-level coverage of required_skill_coverage() and
# required_skill_coverage_below_floor() -- the raw counts/ratio and the
# 60% boundary check, independent of any decision-capping (that's
# integration-tested end-to-end in test_scorer.py). Letters mirror the
# approved product examples.


def test_A_5_of_5_required_no_gate():
    profile = _profile(["a", "b", "c", "d", "e"])
    extraction = _extraction(required=["a", "b", "c", "d", "e"])
    coverage = required_skill_coverage(extraction, profile.skills)
    assert coverage == (5, 5, 1.0)
    assert required_skill_coverage_below_floor(coverage) is False


def test_B_4_of_5_required_no_gate():
    profile = _profile(["a", "b", "c", "d"])
    extraction = _extraction(required=["a", "b", "c", "d", "e"])
    coverage = required_skill_coverage(extraction, profile.skills)
    assert coverage.matched == 4 and coverage.total == 5
    assert required_skill_coverage_below_floor(coverage) is False


def test_C_3_of_5_required_exactly_60_percent_no_gate():
    """Exact boundary (also M): 3/5 == 60% must NOT trigger the gate --
    the product rule is strictly '< 60%', not '<= 60%'."""
    profile = _profile(["a", "b", "c"])
    extraction = _extraction(required=["a", "b", "c", "d", "e"])
    coverage = required_skill_coverage(extraction, profile.skills)
    assert coverage == (3, 5, 0.6)
    assert required_skill_coverage_below_floor(coverage) is False


def test_D_2_of_5_required_gate_triggers():
    profile = _profile(["a", "b"])
    extraction = _extraction(required=["a", "b", "c", "d", "e"])
    coverage = required_skill_coverage(extraction, profile.skills)
    assert coverage == (2, 5, 0.4)
    assert required_skill_coverage_below_floor(coverage) is True


def test_E_1_of_5_required_gate_triggers():
    profile = _profile(["a"])
    extraction = _extraction(required=["a", "b", "c", "d", "e"])
    coverage = required_skill_coverage(extraction, profile.skills)
    assert coverage == (1, 5, 0.2)
    assert required_skill_coverage_below_floor(coverage) is True


def test_F_0_of_5_required_gate_triggers():
    profile = _profile([])
    extraction = _extraction(required=["a", "b", "c", "d", "e"])
    coverage = required_skill_coverage(extraction, profile.skills)
    assert coverage == (0, 5, 0.0)
    assert required_skill_coverage_below_floor(coverage) is True


def test_I_required_empty_preferred_6_of_29_returns_none_no_gate():
    """Reproduces the Spectrum-style case: required=[] means there is no
    confirmed-mandatory signal to gate on at all, regardless of how weak
    the (optional) preferred coverage is."""
    preferred = [f"skill{i}" for i in range(29)]
    matched = [f"skill{i}" for i in range(6)]
    profile = _profile(matched)
    extraction = _extraction(required=[], preferred=preferred)
    assert required_skill_coverage(extraction, profile.skills) is None


def test_J_required_empty_preferred_7_of_17_returns_none_no_gate():
    """Reproduces the Wabtec-style case: same -- required=[] means no
    gate, independent of the preferred ratio."""
    preferred = [f"skill{i}" for i in range(17)]
    matched = [f"skill{i}" for i in range(7)]
    profile = _profile(matched)
    extraction = _extraction(required=[], preferred=preferred)
    assert required_skill_coverage(extraction, profile.skills) is None


def test_K_large_preferred_list_does_not_affect_the_required_gate():
    """4/5 required (80%, no gate) plus a 40-item preferred list that is
    almost entirely unmatched must not move the required-only gate at
    all -- it never looks at preferred_skills."""
    required = ["a", "b", "c", "d", "e"]
    preferred = [f"pref{i}" for i in range(40)]
    profile = _profile(["a", "b", "c", "d"])  # 4/5 required, 0/40 preferred
    extraction = _extraction(required=required, preferred=preferred)
    coverage = required_skill_coverage(extraction, profile.skills)
    assert coverage == (4, 5, 0.8)
    assert required_skill_coverage_below_floor(coverage) is False


def test_M_exactly_60_percent_boundary_various_denominators_no_gate():
    """The exact-60% boundary must hold for denominators other than 5
    too (integer cross-multiplication, not float ratio comparison)."""
    profile = _profile(["a", "b", "c"])
    extraction = _extraction(required=["a", "b", "c", "d", "e"])
    coverage = required_skill_coverage(extraction, profile.skills)  # 3/5
    assert required_skill_coverage_below_floor(coverage) is False


def test_N_just_below_60_percent_gate_triggers():
    """7/12 = 58.33...% -- just under the 60% floor."""
    required = [f"req{i}" for i in range(12)]
    matched = [f"req{i}" for i in range(7)]
    profile = _profile(matched)
    extraction = _extraction(required=required)
    coverage = required_skill_coverage(extraction, profile.skills)
    assert coverage.matched == 7 and coverage.total == 12
    assert required_skill_coverage_below_floor(coverage) is True


def test_required_skill_coverage_message_matches_product_wording():
    profile = _profile(["a", "b"])
    extraction = _extraction(required=["a", "b", "c", "d", "e"])
    coverage = required_skill_coverage(extraction, profile.skills)
    message = format_required_skill_coverage_message(coverage)
    assert message == (
        "⚠️ Required skill coverage is 40% (2/5). "
        "Review required skills before applying."
    )
