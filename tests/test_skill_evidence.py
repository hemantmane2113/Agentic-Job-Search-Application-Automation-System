"""
Hybrid skill-source architecture: finalized claim-strength precedence
policy (2026-09-11 precedence-policy review + implementation).
Deterministic candidate-vocabulary recovery, deterministic
required/preferred language markers, and the tier-based, conflict-
preserving evidence merge in jobs/skill_evidence.py. Fully standalone
-- nothing here touches scoring, the 60% required-skill floor,
weights, thresholds, experience logic, or JobExtraction persistence.
"""

from __future__ import annotations

from naukri_agent.jobs.skill_evidence import (
    TIER_CONTENT_ONLY,
    TIER_DETERMINISTIC_MARKER,
    TIER_LLM,
    TIER_WEAK_DEFAULT,
    MergedSkillEvidence,
    SkillEvidence,
    build_skill_evidence,
    classify_skill_sentence,
    evidence_from_key_skills_dom,
    evidence_from_ld_json_skills,
    evidence_from_llm,
    merge_skill_evidence,
    merged_required_preferred,
    recover_vocabulary_skills,
)
from naukri_agent.matching.skill_normalizer import normalize_skill

CANDIDATE_SKILLS = [
    "Python", "SQL", "R", "Machine Learning", "Deep Learning", "Generative AI",
    "LLM", "RAG", "NLP", "Computer Vision", "PyTorch", "TensorFlow", "Keras",
    "scikit-learn", "Pandas", "NumPy", "Matplotlib", "OpenCV",
    "Hugging Face Transformers", "spaCy", "MySQL", "Git", "FastAPI",
    "BeautifulSoup", "Selenium", "Scrapy",
]

# Real, persisted job.description text (Run 19, jobs 66 and 8) -- copied
# verbatim so these tests exercise the actual scraped artefacts (e.g.
# "machinelearning" glued with no separator at all).
GENERAC_DESCRIPTION = """Primary Purpose

The Data Scientist Support role provides ongoing operational and analytical support for Agentic AI and Conversational AI solutions, ensuring their stability, accuracy, and effective performance in production environments. This role works under the guidance of senior data scientists, AI engineers, and platform leads to support model behavior, monitor system performance, analyze interaction data, and assist with tuning, testing, and validation activities.

Key focus areas include:
Monitoring and supporting Agentic AI and Conversational AI model performance and behaviors
Assisting with analysis of conversation logs, feedback signals, and usage patterns
Supporting model tuning, prompt refinement, and regression testing under guidance
Documenting configurations, changes, and known issues
Adhering to data quality, governance, security, and responsible AI standards

Major Responsibilities

The position focuses on supporting established AI workflows, applying foundational data science and analytics techniques, and executing welldefined support tasks that help maintain reliable, scalable, and compliant AI solutions. The role emphasizes continuous learning, adherence to standards and best practices, and contributing to sustained business value while upholding data quality, governance, security, and responsible AI expectations.
Enable additional Agentic AI and conversational AI solutions based on pre-defined model templates

Education

Bachelors degree in Data Science, Computer Science, Statistics, Mathematics, Engineering, Analytics, or a related field (or equivalent practical experience).

Certification / License

MS-Copilot or equivalent is preferred

Work Experience

02 years of experience in data science, analytics, or a related analytical role (internships, coops, or academic projects acceptable).
Demonstrated experience applying analytical or statistical techniques to realworld datasets.
Experience with building agents using Microsoft CoPilot, and other solution paths or equivalent
Familiarity with Databricks, Microsoft, AWS AI tech stack
Relational database experience & Experience query databases (ex: SQL, MySql).
Experience with one or more statistical analysis tools (ex: MatLab, MiniTab, SPSS, or R).
Experience with statistical analysis languages (ex: R, Python, SQL).
Experience on using the cloud platform (ex Azure, AWS..)

Knowledge / Skills / Abilities
Foundational knowledge of statistics, probability, and basic machinelearning concepts.
Experience using SQL and at least one programming language such as Python or R for data analysis.
Familiarity with Agentic AI, Chatbots, Operational AI applications
Ability to follow defined processes, standards, and documentation practices.
Strong analytical thinking, problemsolving skills, and attention to detail.
Effective written and verbal communication skills.
Familiarity with version control tools (e.g., Git) and collaborative development practices.
Exposure to data pipelines, feature engineering, or model monitoring concepts.
Ability to clearly explain analytical results and recommendations to stakeholders.
Strong desire to learn, grow, and take on increasing responsibility over time."""

ORIGIN_HR_DESCRIPTION = """Master s or Ph.D. degree in Statistics, Mathematics, Computer Science, Data Science, (or equivalent practical experience)

 Strong coding skills in Python or R , with extensive use of scientific computing and data science libraries

 Deep theoretical and practical understanding of machine learning algorithms, statistical inference, hypothesis testing, and probability.

 Proficiency in SQL for data extraction and manipulation from large relational databases or data warehouses.

 Experience with deep learning frameworks or NLP / Generative AI techniques.

 Track record of deploying models into production environments and tracking them via MLOps pipelines.

Disclaimer: This job posting has been aggregated from external source. Role details, content, and availability are subject to change. Applicants are advised to confirm the latest information directly on the company website before applying."""


class _Chip:
    """Duck-typed stand-in for browser.models.KeySkillChip -- proves
    evidence_from_key_skills_dom() has no import-time dependency on
    browser/."""

    def __init__(self, text: str, preferred: bool) -> None:
        self.text = text
        self.preferred = preferred


# --- 1. Key Skills DOM: content-only, no classification authority ----------
#
# REMOVED 2026-09-11 (Run 20 empirical validation): preferred=True used
# to assert "preferred" at Tier 0. A 40-observation real-job sample
# found Naukri's preferred flag does not reliably reflect the JD's own
# required-vs-preferred semantics (78.3% of preferred=True observations
# were explicitly REQUIRED by the JD, 0% were explicitly preferred).
# Both preferred=True and preferred=False are now content-only, tier 4,
# always "unclassified" -- the raw flag is still read/persisted exactly
# as observed (JobRawSkillEvidence, untouched) and still contributes to
# contributing_sources.


def test_dom_preferred_true_is_content_only_not_a_classification():
    evidence = evidence_from_key_skills_dom([_Chip("Python", True)])
    assert len(evidence) == 1
    assert evidence[0].classification == "unclassified"
    assert evidence[0].tier == TIER_CONTENT_ONLY


def test_dom_preferred_false_is_content_only_unclassified():
    """DOM preferred=False was already unclassified; unchanged here --
    both True and False now share the same content-only treatment."""
    evidence = evidence_from_key_skills_dom([_Chip("AWS", False)])
    assert len(evidence) == 1
    assert evidence[0].classification == "unclassified"
    assert evidence[0].tier == TIER_CONTENT_ONLY


# --- 12. required/preferred marker classification (unchanged) --------------


def test_required_marker_classifies_required():
    assert classify_skill_sentence("Experience with SQL for data extraction.") == "required"
    assert classify_skill_sentence("Proficiency in Python is required.") == "required"


def test_preferred_marker_classifies_preferred():
    assert classify_skill_sentence("Familiarity with Databricks is a plus.") == "preferred"
    assert classify_skill_sentence("Exposure to Kubernetes is preferred.") == "preferred"


def test_no_marker_at_all_is_unclassified_never_guessed():
    assert classify_skill_sentence("Kubernetes") == "unclassified"
    assert classify_skill_sentence(
        "Track record of deploying models via MLOps pipelines."
    ) == "unclassified"


# --- Deterministic vocabulary recovery: tiering -----------------------------


def test_vocabulary_recovery_with_explicit_marker_is_tier_1():
    evidence = recover_vocabulary_skills(
        "Required Skills\nStrong experience with Git for version control.", ["Git"]
    )
    assert len(evidence) == 1
    assert evidence[0].classification == "required"
    assert evidence[0].tier == TIER_DETERMINISTIC_MARKER


def test_vocabulary_recovery_section_state_only_is_tier_3():
    text = "Required Skills\nGit"  # no explicit marker on the "Git" line itself
    evidence = recover_vocabulary_skills(text, ["Git"])
    assert len(evidence) == 1
    assert evidence[0].classification == "required"
    assert evidence[0].tier == TIER_WEAK_DEFAULT


def test_vocabulary_recovery_no_marker_no_section_is_unclassified():
    evidence = recover_vocabulary_skills("We use Git here.", ["Git"])
    assert len(evidence) == 1
    assert evidence[0].classification == "unclassified"
    assert evidence[0].tier == TIER_CONTENT_ONLY


def test_never_recovers_a_skill_not_in_candidate_vocabulary():
    evidence = recover_vocabulary_skills(
        "Experience with Databricks and AWS required.", ["Python"]
    )
    assert evidence == []


def test_never_infers_a_related_skill_python_does_not_imply_django():
    evidence = recover_vocabulary_skills(
        "Strong Python programming skills required.", ["Django"]
    )
    assert evidence == []


def test_case_insensitive_and_whitespace_tolerant_matching():
    evidence = recover_vocabulary_skills(
        "Required: experience with   PYTHON   programming.", ["Python"]
    )
    assert len(evidence) == 1


def test_hyphenated_form_machine_learning_matches():
    evidence = recover_vocabulary_skills(
        "Required: solid grasp of machine-learning fundamentals.", ["Machine Learning"]
    )
    assert len(evidence) == 1


def test_real_generac_glued_form_machinelearning_matches():
    assert "machinelearning" in GENERAC_DESCRIPTION.lower()
    evidence = recover_vocabulary_skills(GENERAC_DESCRIPTION, ["Machine Learning"])
    assert len(evidence) == 1


def test_short_skill_r_matches_a_genuine_standalone_mention():
    evidence = recover_vocabulary_skills(
        "Experience with statistical analysis languages (ex: R, Python, SQL).", ["R"]
    )
    assert len(evidence) == 1
    assert evidence[0].skill == "R"


def test_short_skill_r_does_not_false_positive_inside_r_and_d_or_hr():
    evidence = recover_vocabulary_skills(
        "Coordinate with the R&D and HR departments on hiring.", ["R"]
    )
    assert evidence == []


def test_negated_mention_is_not_treated_as_positive_evidence():
    evidence = recover_vocabulary_skills(
        "Candidates should note we do not use MySQL in this stack.", ["MySQL"]
    )
    assert evidence == []


def test_negated_mention_does_not_block_a_genuine_later_mention():
    text = "We do not use Kubernetes here.\nRequired: experience with Git."
    evidence = recover_vocabulary_skills(text, ["Git"])
    assert len(evidence) == 1
    assert evidence[0].skill == "Git"


# --- Evidence builders --------------------------------------------------------


def test_evidence_from_llm_tags_required_and_preferred_tier_2():
    evidence = evidence_from_llm(["Python", "SQL"], ["AWS"])
    assert SkillEvidence("Python", "required", "llm", TIER_LLM) in evidence
    assert SkillEvidence("SQL", "required", "llm", TIER_LLM) in evidence
    assert SkillEvidence("AWS", "preferred", "llm", TIER_LLM) in evidence


def test_evidence_from_ld_json_skills_is_always_unclassified_tier_4():
    evidence = evidence_from_ld_json_skills(["Python", "AWS"])
    assert all(e.classification == "unclassified" for e in evidence)
    assert all(e.tier == TIER_CONTENT_ONLY for e in evidence)
    assert all(e.source == "ld_json" for e in evidence)


def test_evidence_from_key_skills_dom_handles_none_and_empty():
    assert evidence_from_key_skills_dom(None) == []
    assert evidence_from_key_skills_dom([]) == []


# --- Merge: precedence, conflicts, corroboration, no double counting -------


def test_explicit_deterministic_required_marker_beats_llm_preferred():
    evidence = [
        SkillEvidence("MLOps", "preferred", "llm", TIER_LLM),
        SkillEvidence("MLOps", "required", "deterministic_vocabulary", TIER_DETERMINISTIC_MARKER),
    ]
    merged = merge_skill_evidence(evidence)
    assert len(merged) == 1
    assert merged[0].classification == "required"
    assert merged[0].source == "deterministic_vocabulary"
    assert merged[0].classification_conflict is True
    assert ("preferred", "llm") in merged[0].conflicting_classifications


def test_explicit_deterministic_preferred_marker_beats_llm_required():
    evidence = [
        SkillEvidence("Databricks", "required", "llm", TIER_LLM),
        SkillEvidence(
            "Databricks", "preferred", "deterministic_vocabulary", TIER_DETERMINISTIC_MARKER
        ),
    ]
    merged = merge_skill_evidence(evidence)
    assert merged[0].classification == "preferred"
    assert merged[0].source == "deterministic_vocabulary"
    assert merged[0].classification_conflict is True


def test_dom_preferred_true_does_not_override_llm_required():
    """REMOVED (Run 20 empirical validation): DOM preferred=True no
    longer beats anything -- it's content-only now. LLM's 'required'
    stands untouched, and no conflict is recorded since DOM's claim is
    'unclassified' and never competes."""
    evidence = evidence_from_llm(["Python"], []) + evidence_from_key_skills_dom(
        [_Chip("Python", True)]
    )
    merged = merge_skill_evidence(evidence)
    assert len(merged) == 1
    assert merged[0].classification == "required"
    assert merged[0].source == "llm"
    assert merged[0].classification_conflict is False
    assert "key_skills_dom" in merged[0].contributing_sources  # still corroborates


def test_weak_no_icon_dom_does_not_override_llm():
    """DOM preferred=False is 'unclassified' -- it never competes for
    classification at all, so LLM's verdict stands untouched and no
    conflict is recorded."""
    evidence = evidence_from_llm(["AWS"], []) + evidence_from_key_skills_dom(
        [_Chip("AWS", False)]
    )
    merged = merge_skill_evidence(evidence)
    assert len(merged) == 1
    assert merged[0].classification == "required"
    assert merged[0].source == "llm"
    assert merged[0].classification_conflict is False
    # but the DOM's raw content presence is still visible as corroboration
    assert "key_skills_dom" in merged[0].contributing_sources


def test_ld_json_alone_remains_unclassified_after_merge():
    evidence = evidence_from_ld_json_skills(["Databricks"])
    merged = merge_skill_evidence(evidence)
    assert len(merged) == 1
    assert merged[0].classification == "unclassified"
    assert merged[0].classification_conflict is False


def test_vocabulary_recovery_adds_a_missing_candidate_skill():
    raw, merged = build_skill_evidence(
        required_skills=["SQL"], description="Experience with Git required.",
        candidate_skills=["SQL", "Git"],
    )
    merged_skills = {normalize_skill(m.skill) for m in merged}
    assert normalize_skill("Git") in merged_skills


def test_duplicate_skill_from_three_plus_sources_counts_once():
    evidence = [
        SkillEvidence("Python", "required", "llm", TIER_LLM),
        SkillEvidence("Python", "unclassified", "ld_json", TIER_CONTENT_ONLY),
        SkillEvidence("Python", "unclassified", "key_skills_dom", TIER_CONTENT_ONLY),
        SkillEvidence(
            "Python", "required", "deterministic_vocabulary", TIER_DETERMINISTIC_MARKER
        ),
    ]
    merged = merge_skill_evidence(evidence)
    assert len(merged) == 1
    assert set(merged[0].contributing_sources) == {
        "llm", "ld_json", "key_skills_dom", "deterministic_vocabulary"
    }


def test_conflicting_classifications_remain_visible():
    """DOM no longer produces classified claims, so this now uses the
    deterministic-marker-vs-LLM conflict pair to prove disagreement
    stays auditable."""
    evidence = [
        SkillEvidence("MLOps", "required", "llm", TIER_LLM),
        SkillEvidence(
            "MLOps", "preferred", "deterministic_vocabulary", TIER_DETERMINISTIC_MARKER
        ),
    ]
    merged = merge_skill_evidence(evidence)
    assert merged[0].classification_conflict is True
    assert merged[0].conflicting_classifications == (("required", "llm"),)
    # the winner itself is not listed among its own "losing" claims
    assert ("preferred", "deterministic_vocabulary") not in merged[0].conflicting_classifications


def test_agreeing_sources_produce_no_conflict():
    evidence = [
        SkillEvidence("Python", "required", "llm", TIER_LLM),
        SkillEvidence(
            "Python", "required", "deterministic_vocabulary", TIER_DETERMINISTIC_MARKER
        ),
    ]
    merged = merge_skill_evidence(evidence)
    assert merged[0].classification_conflict is False
    assert merged[0].conflicting_classifications == ()
    assert set(merged[0].contributing_sources) == {"llm", "deterministic_vocabulary"}


def test_winning_classification_is_deterministic_regardless_of_input_order():
    a = [
        SkillEvidence("MLOps", "required", "llm", TIER_LLM),
        SkillEvidence(
            "MLOps", "preferred", "deterministic_vocabulary", TIER_DETERMINISTIC_MARKER
        ),
    ]
    b = list(reversed(a))
    merged_a = merge_skill_evidence(a)
    merged_b = merge_skill_evidence(b)
    assert merged_a[0].classification == merged_b[0].classification == "preferred"


def test_contributing_sources_retained_even_when_unclassified_lost():
    evidence = [
        SkillEvidence("AWS", "required", "llm", TIER_LLM),
        SkillEvidence("AWS", "unclassified", "ld_json", TIER_CONTENT_ONLY),
    ]
    merged = merge_skill_evidence(evidence)
    assert merged[0].classification == "required"
    assert set(merged[0].contributing_sources) == {"llm", "ld_json"}
    assert merged[0].classification_conflict is False  # unclassified never conflicts


def test_skill_inclusion_is_a_union_never_dropped_for_being_unclassified():
    """Content vs classification: a skill mentioned only by ld_json
    (no classification anywhere) must still appear in the merged
    output -- inclusion is not gated by having a verdict."""
    _raw, merged = build_skill_evidence(ld_json_skills=["Databricks"])
    assert any(m.skill == "Databricks" and m.classification == "unclassified" for m in merged)


def test_merge_never_increases_count_for_three_sources_naming_ten_skills_once_each():
    llm_req = [f"skill{i}" for i in range(10)]
    ld_json = [f"skill{i}" for i in range(10)]
    dom = [_Chip(f"skill{i}", preferred=False) for i in range(10)]
    raw = (
        evidence_from_llm(llm_req, [])
        + evidence_from_ld_json_skills(ld_json)
        + evidence_from_key_skills_dom(dom)
    )
    assert len(raw) == 30  # full audit trail keeps every source's claim
    merged = merge_skill_evidence(raw)
    assert len(merged) == 10  # never double-counted


def test_merge_skill_alias_forms_collapse_via_normalize_skill():
    evidence = [
        SkillEvidence("ML", "unclassified", "ld_json", TIER_CONTENT_ONLY),
        SkillEvidence("Machine Learning", "required", "llm", TIER_LLM),
    ]
    merged = merge_skill_evidence(evidence)
    assert len(merged) == 1
    assert merged[0].classification == "required"


# --- Backward-compatible JobExtraction-shaped projection --------------------


def test_merged_required_preferred_round_trips_pure_llm_input():
    """When only the LLM has ever contributed evidence (today's
    behavior, before any of the new sources are wired in), the merged
    projection must reproduce EXACTLY the same (required, preferred)
    lists that were passed in -- proving this is backward compatible
    with existing JobExtraction data."""
    required_in = ["Python", "SQL", "Machine Learning"]
    preferred_in = ["AWS", "Docker"]
    _raw, merged = build_skill_evidence(required_skills=required_in, preferred_skills=preferred_in)
    required_out, preferred_out = merged_required_preferred(merged)
    assert sorted(required_out) == sorted(required_in)
    assert sorted(preferred_out) == sorted(preferred_in)


def test_merged_required_preferred_can_populate_job_extraction_create():
    """Proves the integration POINT works: merged_required_preferred()'s
    output is directly assignable to JobExtractionCreate.required_skills
    / preferred_skills with no shape conversion needed. This does NOT
    wire skill_evidence into JobParser/orchestration -- it only proves
    the shapes are compatible, per this phase's scope (see the report's
    schema section)."""
    from naukri_agent.jobs.models import JobExtractionCreate

    _raw, merged = build_skill_evidence(
        required_skills=["SQL", "Python", "R"],
        preferred_skills=["Agentic AI"],
        description=GENERAC_DESCRIPTION,
        candidate_skills=CANDIDATE_SKILLS,
    )
    required_out, preferred_out = merged_required_preferred(merged)
    extraction = JobExtractionCreate(
        required_skills=required_out,
        preferred_skills=preferred_out,
    )
    assert set(extraction.required_skills) >= {"SQL", "Python", "R"}
    assert "Agentic AI" in extraction.preferred_skills


def test_merged_required_preferred_excludes_unclassified():
    _raw, merged = build_skill_evidence(ld_json_skills=["Databricks"], required_skills=["Python"])
    required_out, preferred_out = merged_required_preferred(merged)
    assert required_out == ["Python"]
    assert preferred_out == []
    assert "Databricks" not in required_out and "Databricks" not in preferred_out


# --- Generac / Origin HR: expected evidence ---------------------------------


def test_generac_expected_evidence_includes_all_listed_skills():
    """Item 9: verify the system can REPRESENT evidence for every one
    of these -- not that every one becomes required/preferred. Content
    must never be silently lost."""
    _raw, merged = build_skill_evidence(
        required_skills=["SQL", "Python", "R"],
        preferred_skills=["Agentic AI", "Chatbots", "Operational AI"],
        ld_json_skills=["SQL", "Python", "R", "AWS", "Azure", "Databricks", "Agentic AI"],
        description=GENERAC_DESCRIPTION,
        candidate_skills=CANDIDATE_SKILLS,
    )
    merged_skills = {normalize_skill(m.skill) for m in merged}
    expected = [
        "SQL", "Python", "R", "Machine Learning", "MySQL", "Git",
        "AWS", "Azure", "Databricks", "Agentic AI", "Chatbots", "Operational AI",
    ]
    for skill in expected:
        assert normalize_skill(skill) in merged_skills, f"{skill} missing from merged evidence"


def test_generac_recovers_candidate_known_missed_skills_via_vocabulary():
    _raw, merged = build_skill_evidence(
        required_skills=["SQL", "Python", "R"],
        preferred_skills=["Agentic AI", "Chatbots", "Operational AI"],
        description=GENERAC_DESCRIPTION,
        candidate_skills=CANDIDATE_SKILLS,
    )
    merged_by_skill = {normalize_skill(m.skill): m for m in merged}
    for expected in ("Machine Learning", "MySQL", "Git"):
        key = normalize_skill(expected)
        assert key in merged_by_skill
        assert merged_by_skill[key].source == "deterministic_vocabulary"


def test_generac_naukri_source_exposes_non_candidate_skills_illustrative():
    """AWS/Azure/Databricks are real Generac requirements the candidate
    does not know -- only a Naukri-sourced input can expose them
    (deterministic vocabulary structurally cannot). No real ld+json/
    Key-Skills capture exists for this specific job (live browser
    access to Naukri was blocked this session) -- this ld_json_skills
    value is an ILLUSTRATIVE, clearly-labeled construction."""
    _raw, merged = build_skill_evidence(
        required_skills=["SQL", "Python", "R"],
        ld_json_skills=["SQL", "Python", "R", "AWS", "Azure", "Databricks"],
        description=GENERAC_DESCRIPTION,
        candidate_skills=CANDIDATE_SKILLS,
    )
    merged_by_skill = {normalize_skill(m.skill): m for m in merged}
    for expected in ("AWS", "Azure", "Databricks"):
        key = normalize_skill(expected)
        assert merged_by_skill[key].source == "ld_json"
        assert merged_by_skill[key].classification == "unclassified"


def test_origin_hr_expected_evidence():
    """All 8 requested skills must classify correctly. The WINNING
    source varies by design: when deterministic vocabulary independently
    finds an explicit marker for a skill (e.g. "Proficiency in SQL",
    "Experience with deep learning frameworks... NLP / Generative AI"),
    that grounded tier-1 evidence legitimately outranks the LLM's tier-2
    classification even though both AGREE -- this is intended
    corroboration, not a conflict (classification_conflict must stay
    False whenever sources agree, regardless of which one is recorded
    as the winning source)."""
    _raw, merged = build_skill_evidence(
        required_skills=[
            "Python", "R", "SQL", "Machine Learning", "Deep Learning", "NLP", "Generative AI",
        ],
        preferred_skills=["MLOps"],
        description=ORIGIN_HR_DESCRIPTION,
        candidate_skills=CANDIDATE_SKILLS,
    )
    merged_by_skill = {normalize_skill(m.skill): m for m in merged}
    for skill in ("Python", "R", "SQL", "Machine Learning", "Deep Learning", "NLP", "Generative AI"):
        key = normalize_skill(skill)
        assert merged_by_skill[key].classification == "required"
        assert merged_by_skill[key].source in ("llm", "deterministic_vocabulary")
        assert merged_by_skill[key].classification_conflict is False  # both sources agree
        assert "llm" in merged_by_skill[key].contributing_sources
    mlops_key = normalize_skill("MLOps")
    assert merged_by_skill[mlops_key].classification == "preferred"
    assert merged_by_skill[mlops_key].source == "llm"  # "do not blindly overwrite LLM"
    assert merged_by_skill[mlops_key].classification_conflict is False  # nothing else had an opinion


def test_origin_hr_statistical_terms_remain_unrepresented():
    """Investigation finding, reconfirmed: statistical inference /
    hypothesis testing / probability are NOT in the candidate's own
    vocabulary, so deterministic_vocabulary structurally cannot
    recover them either -- they remain entirely unrepresented."""
    _raw, merged = build_skill_evidence(
        required_skills=["Python", "R", "SQL"],
        description=ORIGIN_HR_DESCRIPTION,
        candidate_skills=CANDIDATE_SKILLS,
    )
    merged_skills = {normalize_skill(m.skill) for m in merged}
    for missed in ("statistical inference", "hypothesis testing", "probability"):
        assert normalize_skill(missed) not in merged_skills


def test_origin_hr_mlops_sentence_alone_is_unclassified_by_markers():
    sentence = (
        "Track record of deploying models into production environments "
        "and tracking them via MLOps pipelines."
    )
    assert classify_skill_sentence(sentence) == "unclassified"


# --- Run 20 regression: DOM preferred flag must not affect classification --
#
# Empirical finding (2026-09-11 Run 20 validation): the SAME recruiter
# posted byte-identical JD text twice (Crescendo Global, jobs 83 and
# 84) and Naukri tagged "Machine Learning" preferred=False in one
# posting and preferred=True in the other. If the DOM flag still had
# classification authority, identical inputs could silently produce
# different classifications depending on which repost happened to be
# fetched. This must be structurally impossible now: with everything
# else held constant, only the DOM preferred flag differing must never
# change the merged classification.


def test_run20_identical_jd_classifies_consistently_regardless_of_dom_flag():
    common = dict(
        required_skills=["Machine Learning"],
        description=(
            "Requirements\n"
            "3-4 years of professional experience in Data Science, "
            "Applied Machine Learning, or Computer Vision."
        ),
        candidate_skills=["Machine Learning"],
    )
    _raw_a, merged_a = build_skill_evidence(
        **common, key_skills_dom=[_Chip("Machine Learning", True)]
    )
    _raw_b, merged_b = build_skill_evidence(
        **common, key_skills_dom=[_Chip("Machine Learning", False)]
    )

    a = {normalize_skill(m.skill): m for m in merged_a}["machine learning"]
    b = {normalize_skill(m.skill): m for m in merged_b}["machine learning"]

    assert a.classification == b.classification == "required"
    assert a.classification_conflict == b.classification_conflict == False  # noqa: E712
    # the raw DOM flag is still visible in contributing_sources either way
    assert "key_skills_dom" in a.contributing_sources
    assert "key_skills_dom" in b.contributing_sources


def test_run20_dom_preferred_true_alone_stays_unclassified_unless_stronger_source_exists():
    """Item (d): DOM preferred=True with NOTHING else classifying the
    skill must resolve to 'unclassified', not silently 'preferred'."""
    _raw, merged = build_skill_evidence(
        ld_json_skills=None,
        key_skills_dom=[_Chip("Kubernetes", True)],
        description="We use Kubernetes here.",  # no candidate/vocabulary match, no LLM opinion
        candidate_skills=[],
    )
    row = {normalize_skill(m.skill): m for m in merged}["kubernetes"]
    assert row.classification == "unclassified"
    assert row.contributing_sources == ("key_skills_dom",)


def test_run20_dom_preferred_false_alone_stays_unclassified():
    _raw, merged = build_skill_evidence(key_skills_dom=[_Chip("Kubernetes", False)])
    row = {normalize_skill(m.skill): m for m in merged}["kubernetes"]
    assert row.classification == "unclassified"


def test_run20_dom_preferred_true_plus_llm_preferred_stays_preferred():
    """Item (c): DOM True + LLM preferred, no deterministic marker."""
    _raw, merged = build_skill_evidence(
        preferred_skills=["Kubernetes"],
        key_skills_dom=[_Chip("Kubernetes", True)],
    )
    row = {normalize_skill(m.skill): m for m in merged}["kubernetes"]
    assert row.classification == "preferred"
    assert row.source == "llm"
    assert row.classification_conflict is False


def test_run20_dom_preferred_false_plus_llm_preferred_stays_preferred():
    """Item (f): DOM False + LLM preferred."""
    _raw, merged = build_skill_evidence(
        preferred_skills=["Kubernetes"],
        key_skills_dom=[_Chip("Kubernetes", False)],
    )
    row = {normalize_skill(m.skill): m for m in merged}["kubernetes"]
    assert row.classification == "preferred"
    assert row.source == "llm"


def test_run20_dom_preferred_false_plus_deterministic_required_stays_required():
    """Item (e): DOM False + an explicit deterministic JD marker."""
    _raw, merged = build_skill_evidence(
        key_skills_dom=[_Chip("Git", False)],
        description="Required: experience with Git for version control.",
        candidate_skills=["Git"],
    )
    row = {normalize_skill(m.skill): m for m in merged}["git"]
    assert row.classification == "required"
    assert row.source == "deterministic_vocabulary"


def test_run20_dom_preferred_true_plus_deterministic_required_stays_required():
    """Item (a): DOM True + an explicit deterministic JD marker -- the
    marker wins, DOM's True flag does NOT pull it to 'preferred'."""
    _raw, merged = build_skill_evidence(
        key_skills_dom=[_Chip("Git", True)],
        description="Required: experience with Git for version control.",
        candidate_skills=["Git"],
    )
    row = {normalize_skill(m.skill): m for m in merged}["git"]
    assert row.classification == "required"
    assert row.source == "deterministic_vocabulary"
    assert row.classification_conflict is False
    assert "key_skills_dom" in row.contributing_sources
