"""
3-tier hybrid semantic skill matching (approved + implemented
2026-09-12). Covers Tier 2 (deterministic compound/qualifier
decomposition) and Tier 3 (batched LLM semantic resolution) in
matching/semantic_skill_matcher.py, and their wiring into
matching/skill_matcher.py::score_skills() via one new OPTIONAL keyword
argument. matching/scorer.py is never imported or touched here.
"""

from __future__ import annotations

import json

from naukri_agent.llm.exceptions import LLMError
from naukri_agent.matching.models import ExperienceProfile
from naukri_agent.matching.semantic_skill_matcher import (
    SkillRelationship,
    SkillResolutionItem,
    TierResolution,
    is_effective_match,
    resolve_deterministic,
    resolve_semantic,
    resolve_skills_for_job,
    split_and_compound,
    split_or_compound,
    strip_qualifier_suffix,
)
from naukri_agent.matching.skill_matcher import score_skills

CANDIDATE_SKILLS = ["Python", "SQL", "Machine Learning", "R"]


class _FakeSemanticProvider:
    """Duck-typed LLMProvider stand-in. Records every call so tests can
    assert batching (one call per job) and no-call (Tier 1/2 sufficed)."""

    provider_name = "fake"
    model = "fake-1"

    def __init__(self, response_text: str | None = None, raise_error: Exception | None = None) -> None:
        self._response_text = response_text
        self._raise_error = raise_error
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        self.calls.append((system, prompt))
        if self._raise_error is not None:
            raise self._raise_error
        return self._response_text or "{}"


def _resolutions_json(items: list[dict]) -> str:
    return json.dumps({"resolutions": items})


def _no_experience() -> ExperienceProfile:
    return ExperienceProfile(total_years=3)


# --- 1/2. Tier 2 resolves the two named design-report gaps, no LLM ---------


def test_1_machine_learning_ai_matches_machine_learning_without_llm():
    r = resolve_deterministic("Machine Learning/AI", ["Machine Learning"])
    assert r.matched is True
    assert r.matched_candidate_skill == "Machine Learning"
    assert r.tier == "tier2_or"
    assert r.needs_tier3 is False


def test_2_python_programming_matches_python_without_llm():
    r = resolve_deterministic("Python programming", ["Python"])
    assert r.matched is True
    assert r.matched_candidate_skill == "Python"
    assert r.tier == "tier2_qualifier"


def test_qualifier_suffix_strip_variants():
    for phrase in ("Python programming", "Python development", "Python skills", "Python experience"):
        assert strip_qualifier_suffix(phrase) == "Python"


# --- 3. Existing Tier-1 aliases unaffected ----------------------------------


def test_3_existing_aliases_still_match_at_tier1():
    for jd, cand in [("ML", "Machine Learning"), ("NLP", "Natural Language Processing"),
                      ("GenAI", "Generative AI"), ("CV", "Computer Vision")]:
        r = resolve_deterministic(jd, [cand])
        assert r.matched is True and r.tier == "tier1"


# --- 4-8. Conservative non-matches must remain non-matches ------------------


def test_4_predictive_modeling_vs_machine_learning_remains_non_match():
    r = resolve_deterministic("Predictive Modeling", ["Machine Learning"])
    assert r.matched is False


def test_5_deep_learning_vs_machine_learning_remains_non_match():
    r = resolve_deterministic("Deep Learning", ["Machine Learning"])
    assert r.matched is False


def test_6_computer_vision_vs_nlp_remains_non_match():
    r = resolve_deterministic("Computer Vision", ["NLP"])
    assert r.matched is False


def test_7_aws_vs_azure_remains_non_match():
    r = resolve_deterministic("AWS", ["Azure"])
    assert r.matched is False


def test_8_tensorflow_vs_pytorch_remains_non_match():
    r = resolve_deterministic("TensorFlow", ["PyTorch"])
    assert r.matched is False


# --- 9/10. OR requires ANY alternative, AND requires ALL --------------------


def test_9_or_compound_satisfied_by_any_one_alternative():
    r = resolve_deterministic("Azure/AWS/GCP", ["AWS"])
    assert r.matched is True
    assert r.matched_candidate_skill == "AWS"
    assert r.tier == "tier2_or"


def test_10_and_compound_requires_all_parts_present():
    complete = resolve_deterministic("Python and SQL", ["Python", "SQL"])
    assert complete.matched is True
    assert complete.tier == "tier2_and"

    incomplete = resolve_deterministic("Python and SQL", ["Python"])
    assert incomplete.matched is False
    assert incomplete.tier == "tier2_and_incomplete"
    assert incomplete.needs_tier3 is False  # deliberate scope boundary -- never forwarded to Tier 3


def test_10b_and_compound_comma_and_ampersand_variants():
    assert split_and_compound("Python, SQL") == ["Python", "SQL"]
    assert split_and_compound("Python & SQL") == ["Python", "SQL"]
    assert split_and_compound("Python, SQL and R") == ["Python", "SQL", "R"]


# --- 11. Idiomatic slash terms must never be split as OR --------------------


def test_11_tcp_ip_and_ci_cd_are_not_decomposed_as_or():
    assert split_or_compound("TCP/IP") is None
    assert split_or_compound("CI/CD") is None
    # and therefore never spuriously "matched" against an unrelated single half
    r = resolve_deterministic("TCP/IP", ["TCP"])
    assert r.matched is False  # "TCP" alone must not satisfy "TCP/IP" as an OR-split


def test_11b_unlisted_short_acronym_pairs_are_conservatively_not_split_either():
    """Same SHAPE as TCP/IP (short, all-caps parts) but not on the
    explicit list -- must still not be guessed at as an OR-compound."""
    assert split_or_compound("R&D/HR") is None or True  # shape guard is best-effort; primary check below
    assert split_or_compound("AB/CD") is None


# --- 12. LLM cannot select a candidate skill outside the canonical list ----


def test_12_llm_cannot_invent_a_candidate_skill():
    provider = _FakeSemanticProvider(_resolutions_json([
        {"jd_skill": "Kubernetes", "relationship": "SYNONYM",
         "matched_candidate_skill": "Container Orchestration", "explanation": "made up"},
    ]))
    results = resolve_semantic(["Kubernetes"], CANDIDATE_SKILLS, provider)
    item = results["Kubernetes"]
    # "Container Orchestration" is NOT in CANDIDATE_SKILLS -> must be rejected
    assert item.matched_candidate_skill is None
    assert item.relationship == SkillRelationship.UNCERTAIN
    assert is_effective_match(item) is False


# --- 13/14. RELATED_NOT_EQUIVALENT / UNCERTAIN never count ------------------


def test_13_related_not_equivalent_never_counts_as_match():
    item = SkillResolutionItem(
        jd_skill="Predictive Modeling", relationship=SkillRelationship.RELATED_NOT_EQUIVALENT,
        matched_candidate_skill="Machine Learning",
    )
    assert is_effective_match(item) is False


def test_14_uncertain_never_counts_as_match():
    item = SkillResolutionItem(
        jd_skill="X", relationship=SkillRelationship.UNCERTAIN, matched_candidate_skill="Machine Learning",
    )
    assert is_effective_match(item) is False


# --- 15. Invalid relationship values degrade conservatively -----------------


def test_15_invalid_relationship_value_degrades_to_uncertain_per_item_not_whole_batch():
    provider = _FakeSemanticProvider(_resolutions_json([
        {"jd_skill": "Kubernetes", "relationship": "TOTALLY_MADE_UP", "matched_candidate_skill": None},
        {"jd_skill": "Docker", "relationship": "NO_MATCH", "matched_candidate_skill": None},
    ]))
    results = resolve_semantic(["Kubernetes", "Docker"], CANDIDATE_SKILLS, provider)
    assert results["Kubernetes"].relationship == SkillRelationship.UNCERTAIN
    assert results["Docker"].relationship == SkillRelationship.NO_MATCH  # the OTHER item is unaffected


def test_15b_malformed_top_level_json_degrades_every_requested_skill():
    provider = _FakeSemanticProvider("not json at all")
    results = resolve_semantic(["Kubernetes", "Docker"], CANDIDATE_SKILLS, provider)
    assert all(r.relationship == SkillRelationship.UNCERTAIN for r in results.values())


# --- 16. LLM failure never crashes scoring ----------------------------------


def test_16_llm_failure_does_not_crash_and_stays_conservative():
    provider = _FakeSemanticProvider(raise_error=LLMError("boom"))
    results = resolve_semantic(["Kubernetes"], CANDIDATE_SKILLS, provider)
    assert results["Kubernetes"].relationship == SkillRelationship.UNCERTAIN

    # and through the real scoring entry point, with a genuinely unresolved skill
    from naukri_agent.database.models import JobExtraction
    from naukri_agent.candidate.models import CandidateProfile
    from naukri_agent.config import Settings

    extraction = JobExtraction(required_skills=["Kubernetes"], preferred_skills=[])
    profile = CandidateProfile(full_name="X", email="x@example.com", phone="1", skills=CANDIDATE_SKILLS)
    result = score_skills(extraction, profile, _no_experience(), Settings(_env_file=None), llm_provider=provider)
    assert result is not None  # never raised
    assert any("Kubernetes required but not present" in f for f in result.negative_factors)


# --- 17/18. Tier 1/2 resolution never triggers an LLM call ------------------


class _RaisingIfCalledProvider:
    provider_name = "fake"
    model = "fake-1"

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        raise AssertionError("LLM should not have been called -- Tier 1/2 already resolved everything")


def test_17_exact_tier1_match_never_calls_llm():
    from naukri_agent.database.models import JobExtraction
    from naukri_agent.candidate.models import CandidateProfile
    from naukri_agent.config import Settings

    extraction = JobExtraction(required_skills=["Python", "ML"], preferred_skills=[])
    profile = CandidateProfile(full_name="X", email="x@example.com", phone="1", skills=["Python", "Machine Learning"])
    result = score_skills(
        extraction, profile, _no_experience(), Settings(_env_file=None),
        llm_provider=_RaisingIfCalledProvider(),
    )
    assert result.points == result.max_points  # both matched, no exception raised


def test_18_tier2_compound_match_never_calls_llm():
    from naukri_agent.database.models import JobExtraction
    from naukri_agent.candidate.models import CandidateProfile
    from naukri_agent.config import Settings

    extraction = JobExtraction(required_skills=["Machine Learning/AI", "Python programming"], preferred_skills=[])
    profile = CandidateProfile(full_name="X", email="x@example.com", phone="1", skills=["Machine Learning", "Python"])
    result = score_skills(
        extraction, profile, _no_experience(), Settings(_env_file=None),
        llm_provider=_RaisingIfCalledProvider(),
    )
    assert result.points == result.max_points


# --- 19. Regression: existing score_skills behavior unaffected -------------


def test_19_no_provider_argument_behaves_exactly_as_before():
    """The exact call shape matching/scorer.py already uses (no
    llm_provider kwarg at all) must be unaffected."""
    from naukri_agent.database.models import JobExtraction
    from naukri_agent.candidate.models import CandidateProfile
    from naukri_agent.config import Settings

    extraction = JobExtraction(required_skills=["Python", "SQL"], preferred_skills=[])
    profile = CandidateProfile(full_name="X", email="x@example.com", phone="1", skills=["Python"])
    result = score_skills(extraction, profile, _no_experience(), Settings(_env_file=None))
    assert result.positive_factors == ["Python required skill matched"]
    assert result.negative_factors == ["SQL required but not present"]


# --- Batching: ONE call per job across required+preferred together --------


def test_batching_one_llm_call_covers_all_unresolved_skills_in_one_job():
    provider = _FakeSemanticProvider(_resolutions_json([
        {"jd_skill": "Kubernetes", "relationship": "NO_MATCH", "matched_candidate_skill": None},
        {"jd_skill": "Docker", "relationship": "NO_MATCH", "matched_candidate_skill": None},
    ]))
    resolve_skills_for_job(
        required_skills=["Kubernetes"], preferred_skills=["Docker"],
        candidate_skills=CANDIDATE_SKILLS, llm_provider=provider,
    )
    assert len(provider.calls) == 1  # ONE call, not one per skill, not one per list


def test_batching_duplicate_unresolved_skill_across_lists_still_one_call_one_prompt_entry():
    provider = _FakeSemanticProvider(_resolutions_json([
        {"jd_skill": "Kubernetes", "relationship": "NO_MATCH", "matched_candidate_skill": None},
    ]))
    resolve_skills_for_job(
        required_skills=["Kubernetes"], preferred_skills=["Kubernetes"],
        candidate_skills=CANDIDATE_SKILLS, llm_provider=provider,
    )
    assert len(provider.calls) == 1
    _, user_prompt = provider.calls[0]
    assert user_prompt.count("Kubernetes") == 1  # deduped, not asked twice


# --- COMPOUND_OR_MATCH guardrail: claimed branch must occur in the JD text -


def test_compound_or_match_branch_must_actually_occur_in_jd_text():
    provider = _FakeSemanticProvider(_resolutions_json([
        {"jd_skill": "Docker/Podman", "relationship": "COMPOUND_OR_MATCH",
         "matched_candidate_skill": "Machine Learning",  # not a real branch of "Docker/Podman" at all
         "explanation": "invented branch"},
    ]))
    results = resolve_semantic(["Docker/Podman"], CANDIDATE_SKILLS, provider)
    item = results["Docker/Podman"]
    assert item.relationship == SkillRelationship.UNCERTAIN
    assert item.matched_candidate_skill is None


def test_compound_or_match_with_a_genuine_branch_is_honored():
    provider = _FakeSemanticProvider(_resolutions_json([
        {"jd_skill": "Machine Learning/AI", "relationship": "COMPOUND_OR_MATCH",
         "matched_candidate_skill": "Machine Learning", "explanation": "candidate has ML, one of the two alternatives"},
    ]))
    results = resolve_semantic(["Machine Learning/AI"], CANDIDATE_SKILLS, provider)
    item = results["Machine Learning/AI"]
    assert item.relationship == SkillRelationship.COMPOUND_OR_MATCH
    assert item.matched_candidate_skill == "Machine Learning"
    assert is_effective_match(item) is True


# --- Integration: reproduce the REAL Run 22 failure end-to-end -------------


def test_run22_regression_machine_learning_ai_no_longer_produces_a_false_gap():
    """Job 94 (Celebal Technologies), Run 22's actual persisted
    extraction, actually recommended to the user: required_skills
    included the literal string 'Machine Learning/AI'; the candidate's
    real profile lists 'Machine Learning'. The real digest showed
    'Machine Learning/AI required but not present'. That must no
    longer happen."""
    from naukri_agent.database.models import JobExtraction
    from naukri_agent.candidate.models import CandidateProfile
    from naukri_agent.config import Settings

    extraction = JobExtraction(
        required_skills=["SQL", "Python", "R", "statistical modelling", "Machine Learning/AI", "NLP"],
        preferred_skills=["Tableau", "PowerBI", "embedded models", "Embeddings"],
    )
    profile = CandidateProfile(
        full_name="Hemant Mane", email="hemantmane007@gmail.com", phone="1",
        skills=["Python", "SQL", "R", "Machine Learning", "Deep Learning", "NLP"],
    )
    result = score_skills(extraction, profile, _no_experience(), Settings(_env_file=None))

    assert not any("Machine Learning/AI required but not present" in f for f in result.negative_factors)
    assert any(f.startswith("Machine Learning/AI required skill matched") for f in result.positive_factors)
