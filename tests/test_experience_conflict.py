"""
Experience conflict detection (Run 17 follow-up).

Covers the deterministic JD-body requirement parser, the bounded
candidate list, and classify_experience_requirements -- all in
jobs/parser.py, consumed by matching/scorer.py's Part A decision.
Candidate = 3 years throughout, matching every worked example in the
approved design.
"""

from __future__ import annotations

from naukri_agent.database.models import Job
from naukri_agent.jobs.parser import (
    classify_experience_requirements,
    format_experience_conflict_message,
    format_experience_exclude_multi_message,
)

CANDIDATE_YEARS = 3.0


def _job(experience_text: str | None = None, description: str = "") -> Job:
    return Job(
        url="https://example.com/1",
        title="Data Scientist",
        company="Acme",
        location="Pune",
        description=description,
        experience_text=experience_text,
        content_fingerprint="fp",
    )


# --- Worked examples A-I ----------------------------------------------------


def test_A_el_shaddai_single_high_minimum_rejects_no_conflict():
    """structured 8-13, JD has no experience statement -> single clear
    minimum, plain REJECT, NO conflict message."""
    job = _job(
        "8 - 13 years",
        "Data Scientist with strong hands-on experience in Python, AI, "
        "Machine Learning, Deep Learning & NLP.",
    )
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "exclude_single"
    assert cls.candidates == [(8.0, 13.0, "Naukri")]


def test_B_talent_corner_conflict_review_with_message():
    """structured 4-5, JD body 'Exp - 3+' -> conflict, one admits (3+),
    REVIEW, message mentions 4-5 and 3+."""
    job = _job(
        "4 - 5 years",
        "Data Scientist - Kandivali\nExp - 3+\n Budget - 10\n"
        " Location - Mumbai,Kandivali onsite",
    )
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "conflict"
    assert cls.candidates == [(4.0, 5.0, "Naukri"), (3.0, None, "JD")]
    msg = format_experience_conflict_message(cls)
    assert "4-5" in msg and "3+" in msg
    assert "Naukri" in msg and "JD" in msg
    assert msg.startswith("⚠️ Experience conflict")
    assert msg.endswith("Review manually.")


def test_C_praxis_conflict_review_exact_product_wording():
    """structured 2-4, JD 'Experience Level: 2-4+ Years' (agrees) +
    'Required Skills ... 5+ years' (excludes) -> conflict, REVIEW,
    message matches the product's own literal template word for word."""
    job = _job(
        "2 - 4 years",
        "Location: Mumbai, Work From Office\n"
        "Experience Level: 2-4+ Years\n"
        "Employment Type: Full-time\n"
        "Required Skills & Qualifications Bachelor's or master's degree\n"
        "5+ years of experience in machine learning engineering or similar roles\n"
        "Strong programming expertise in Python\n"
        "Preferred Qualifications Experience with MLOps tools\n",
    )
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "conflict"
    msg = format_experience_conflict_message(cls)
    assert msg == (
        "⚠️ Experience conflict — Naukri shows 2-4 years, "
        "but JD states 5+ years. Review manually."
    )


def test_D_clean_5_to_8_everywhere_rejects():
    job = _job("5 - 8 years", "Requirements: 5-8 years of experience required.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "exclude_single"


def test_E_clean_3_to_8_everywhere_is_eligible():
    job = _job("3 - 8 years", "Standard job description, nothing unusual.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "admit"


def test_F_3plus_everywhere_is_eligible():
    job = _job("3+ years", "Standard job description, nothing unusual.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "admit"


def test_G_welcome_clause_is_not_a_competing_requirement():
    """'5+ years' (requirement) + 'candidates with 3+ years are welcome'
    (soft) -> REJECT on 5+, the welcome clause never becomes a
    candidate at all."""
    job = _job(
        None,
        "Required Skills\n"
        "5+ years of experience in data science\n"
        "Candidates with 3+ years are welcome to apply\n",
    )
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "exclude_single"
    assert cls.candidates == [(5.0, None, "JD")]


def test_H_preferred_clause_does_not_create_a_conflict():
    """'2-4 years preferred' (soft) + structured '4-6' -> REJECT based
    on 4-6 only; the preferred clause never becomes a candidate."""
    job = _job("4 - 6 years", "2-4 years experience preferred for this role.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "exclude_single"
    assert cls.candidates == [(4.0, 6.0, "Naukri")]


# I. (conflict + high aggregate score -> REVIEW never ACCEPT) is an
# end-to-end score_job scenario -- see test_scorer.py.


# --- Additional required coverage ------------------------------------------


def test_multiple_identical_requirements_no_conflict():
    job = _job("5 - 8 years", "Requirements: 5-8 years of experience required.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "exclude_single"
    assert len({c[0] for c in cls.candidates}) == 1


def test_multiple_equivalent_requirements_no_conflict():
    """Same MINIMUM stated two different ways (a bounded range and an
    open-ended '+') is functionally the same requirement -- not a
    conflict, even though the literal (min, max) tuples differ."""
    job = _job("5 - 8 years", "Requirements: minimum 5+ years of experience.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "exclude_single"
    assert {c[0] for c in cls.candidates} == {5.0}


def test_all_conflicting_requirements_exclude_candidate_rejects():
    """Two genuinely DIFFERENT minimums (5 and 9), both excluding the
    3-yr candidate -> REJECT (exclude_multi), never REVIEW, but the
    explanation names every source found."""
    job = _job("5 - 8 years", "Requirements: minimum 9 years of experience.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "exclude_multi"
    assert cls.admits == []
    msg = format_experience_exclude_multi_message(cls, CANDIDATE_YEARS)
    assert "5-8" in msg and "9+" in msg
    assert "⚠️" not in msg  # reserved for genuine REVIEW-worthy conflicts


def test_bonus_clause_does_not_create_a_conflict():
    job = _job("4 - 6 years", "8+ years experience is a bonus for this role.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "exclude_single"
    assert cls.candidates == [(4.0, 6.0, "Naukri")]


def test_no_experience_information_is_unknown():
    job = _job(None, "A job description with no experience statement at all.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "unknown"
    assert cls.candidates == []


def test_existing_grace_window_is_unchanged_within_grace_admits():
    """3.5 - 0.5 (the approved grace) = 3.0 -- exactly admits."""
    job = _job("3.5 - 8 years", "Standard job description.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "admit"


def test_existing_grace_window_is_unchanged_just_beyond_grace_excludes():
    """3.6 - 0.5 = 3.1 > 3.0 -- just past the grace window, excludes."""
    job = _job("3.6 - 8 years", "Standard job description.")
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.state == "exclude_single"


# --- Section-state and labeling edge cases ----------------------------------


def test_heading_fused_with_first_bullet_still_toggles_section_state():
    """Real-world scraping artefact (Praxis): a heading and its first
    bullet land on ONE line. The following line must still be treated
    as inside the Required section."""
    job = _job(
        None,
        "Required Skills & Qualifications Bachelor's degree in CS\n"
        "5+ years of experience in machine learning\n",
    )
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.candidates == [(5.0, None, "JD")]


def test_long_prose_sentence_with_an_early_hyphen_is_never_a_labeled_statement():
    """Guard against the false-positive risk identified in the design
    investigation: a long sentence containing a hyphenated word early on
    ('Asia-based') must never be treated as a 'Label: value' statement,
    even if it also happens to contain a number+unit somewhere."""
    job = _job(
        None,
        "Praxis Global Alliance is an Asia-based next-generation "
        "management consulting firm serving clients across 40+ countries "
        "with 5 years of proven delivery excellence.",
    )
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.candidates == []  # neither "40+" nor "5 years" qualifies


def test_soft_section_toggles_off_required_state():
    job = _job(
        None,
        "Required Skills\n"
        "Good to Have\n"
        "5+ years of experience with distributed systems\n",
    )
    cls = classify_experience_requirements(job, CANDIDATE_YEARS)
    assert cls.candidates == []  # "Good to Have" turned the section off
