"""
Skill-evidence persistence (2026-09-11 implementation of the approved
persistence design): job_raw_skill_evidence, job_extraction_skill_
evidence, and the two repository functions that write them.

Covers table shape/constraints, versioning/lifecycle rules (raw
replaced, derived immutable per extraction version), transactional
integrity, and real-data regression against the Generac/Origin HR
fixtures. No scoring, weights, thresholds, the 60% floor, experience
logic, cooldown, recommendation limits, application history,
scheduler, or email behavior is touched anywhere in this file.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from naukri_agent.config import Settings
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import (
    Job,
    JobExtractionSkillEvidence,
    JobRawSkillEvidence,
)
from naukri_agent.database.repositories import (
    add_job_extraction,
    add_job_extraction_skill_evidence,
    replace_raw_skill_evidence,
    upsert_job,
)
from naukri_agent.jobs.models import JobCreate, JobExtractionCreate
from naukri_agent.jobs.skill_evidence import build_skill_evidence, merged_required_preferred


def _in_memory_settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite:///:memory:")


def _job(**overrides) -> JobCreate:
    defaults = dict(
        title="Data Scientist",
        company="Acme Corp",
        location="Pune",
        description="Looking for a data scientist with Python and SQL.",
        url="https://www.naukri.com/job-listings-skill-evidence-test-900001",
    )
    defaults.update(overrides)
    return JobCreate(**defaults)


class _Chip:
    def __init__(self, text: str, preferred: bool) -> None:
        self.text = text
        self.preferred = preferred


# --- A/H: table creation, FK, uniqueness -------------------------------


def test_raw_skill_evidence_table_created_and_writable():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        rows = replace_raw_skill_evidence(session, job.id, ld_json_skills=["Python"])
        assert len(rows) == 1
        assert session.query(JobRawSkillEvidence).count() == 1


def test_derived_skill_evidence_table_created_and_writable():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        extraction = add_job_extraction(
            session, job.id, JobExtractionCreate(required_skills=["Python"])
        )
        _raw, merged = build_skill_evidence(required_skills=["Python"])
        rows = add_job_extraction_skill_evidence(session, extraction.id, merged)
        assert len(rows) == 1
        assert session.query(JobExtractionSkillEvidence).count() == 1


def test_raw_skill_evidence_fk_references_job():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        replace_raw_skill_evidence(session, job.id, ld_json_skills=["Python"])
        row = session.query(JobRawSkillEvidence).one()
        assert row.job_id == job.id


def test_derived_skill_evidence_fk_references_job_extraction():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        extraction = add_job_extraction(session, job.id, JobExtractionCreate())
        _raw, merged = build_skill_evidence(required_skills=["SQL"])
        add_job_extraction_skill_evidence(session, extraction.id, merged)
        row = session.query(JobExtractionSkillEvidence).one()
        assert row.job_extraction_id == extraction.id


def test_raw_skill_evidence_unique_constraint_rejects_bypassed_duplicate():
    """The repository function itself de-dupes, so hit the constraint
    directly to prove it's actually enforced at the DB level, not just
    by application code."""
    session_factory = init_db(_in_memory_settings())
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            job, _ = upsert_job(session, _job())
            session.add(JobRawSkillEvidence(job_id=job.id, source="ld_json", skill_text="Python"))
            session.flush()
            session.add(JobRawSkillEvidence(job_id=job.id, source="ld_json", skill_text="Python"))
            session.flush()


def test_derived_skill_evidence_unique_constraint_rejects_bypassed_duplicate():
    session_factory = init_db(_in_memory_settings())
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            job, _ = upsert_job(session, _job())
            extraction = add_job_extraction(session, job.id, JobExtractionCreate())
            session.add(JobExtractionSkillEvidence(
                job_extraction_id=extraction.id, skill="Python", skill_key="python",
                classification="required", winning_source="llm",
            ))
            session.flush()
            session.add(JobExtractionSkillEvidence(
                job_extraction_id=extraction.id, skill="python", skill_key="python",
                classification="preferred", winning_source="ld_json",
            ))
            session.flush()


# --- Nullable preferred semantics -------------------------------------


def test_ld_json_raw_rows_have_preferred_null():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        replace_raw_skill_evidence(session, job.id, ld_json_skills=["AWS"])
        row = session.query(JobRawSkillEvidence).filter_by(source="ld_json").one()
        assert row.preferred is None


def test_dom_raw_rows_preserve_true_and_false():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        replace_raw_skill_evidence(
            session, job.id,
            key_skills_dom=[_Chip("Python", True), _Chip("AWS", False)],
        )
        rows = {r.skill_text: r.preferred for r in session.query(JobRawSkillEvidence).all()}
        assert rows == {"Python": True, "AWS": False}


def test_raw_spelling_preserved_verbatim():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        replace_raw_skill_evidence(session, job.id, ld_json_skills=["MySql", "scikit-Learn"])
        texts = {r.skill_text for r in session.query(JobRawSkillEvidence).all()}
        assert texts == {"MySql", "scikit-Learn"}  # NOT normalized/lowercased


# --- skill_key normalization / duplicate merged skill prevention -------


def test_skill_key_is_normalized_form():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        extraction = add_job_extraction(session, job.id, JobExtractionCreate())
        _raw, merged = build_skill_evidence(required_skills=["ML"])  # alias -> "machine learning"
        add_job_extraction_skill_evidence(session, extraction.id, merged)
        row = session.query(JobExtractionSkillEvidence).one()
        assert row.skill == "ML"  # display spelling preserved
        assert row.skill_key == "machine learning"  # normalized key


def test_duplicate_merged_skill_prevented_by_repository_dedup():
    """merge_skill_evidence already guarantees one record per skill, but
    add_job_extraction_skill_evidence defensively de-dupes too -- feed
    it a raw (non-deduped) MergedSkillEvidence list directly to prove
    that defense works even if called incorrectly."""
    from naukri_agent.jobs.skill_evidence import MergedSkillEvidence

    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        extraction = add_job_extraction(session, job.id, JobExtractionCreate())
        not_deduped = [
            MergedSkillEvidence("Python", "required", "llm", ("llm",), False, ()),
            MergedSkillEvidence("python", "required", "llm", ("llm",), False, ()),  # same key
        ]
        rows = add_job_extraction_skill_evidence(session, extraction.id, not_deduped)
        assert len(rows) == 1
        assert session.query(JobExtractionSkillEvidence).count() == 1


# --- C: versioning ------------------------------------------------------


def test_raw_evidence_replaced_not_accumulated_on_refetch():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        replace_raw_skill_evidence(session, job.id, ld_json_skills=["Python", "SQL"])
    with session_scope(session_factory) as session:
        job = session.query(Job).one()
        replace_raw_skill_evidence(session, job.id, ld_json_skills=["AWS", "Azure"])
    with session_scope(session_factory) as session:
        rows = session.query(JobRawSkillEvidence).all()
        assert {r.skill_text for r in rows} == {"AWS", "Azure"}  # old snapshot gone, not accumulated
        assert len(rows) == 2


def test_multiple_extraction_versions_preserve_separate_derived_evidence():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        job_id = job.id

        extraction_v1 = add_job_extraction(session, job_id, JobExtractionCreate())
        _raw1, merged1 = build_skill_evidence(required_skills=["Python"])
        add_job_extraction_skill_evidence(session, extraction_v1.id, merged1)

        extraction_v2 = add_job_extraction(session, job_id, JobExtractionCreate())
        _raw2, merged2 = build_skill_evidence(required_skills=["Python", "SQL"])
        add_job_extraction_skill_evidence(session, extraction_v2.id, merged2)

        v1_skills = {
            r.skill for r in session.query(JobExtractionSkillEvidence)
            .filter_by(job_extraction_id=extraction_v1.id).all()
        }
        v2_skills = {
            r.skill for r in session.query(JobExtractionSkillEvidence)
            .filter_by(job_extraction_id=extraction_v2.id).all()
        }
        assert v1_skills == {"Python"}
        assert v2_skills == {"Python", "SQL"}  # v1's own row set is untouched by v2


def test_derived_evidence_is_immutable_a_later_version_never_updates_an_earlier_ones_rows():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        job_id = job.id
        extraction_v1 = add_job_extraction(session, job_id, JobExtractionCreate())
        _raw1, merged1 = build_skill_evidence(required_skills=["Python"])
        add_job_extraction_skill_evidence(session, extraction_v1.id, merged1)
        v1_row_id = session.query(JobExtractionSkillEvidence).one().id

        extraction_v2 = add_job_extraction(session, job_id, JobExtractionCreate())
        _raw2, merged2 = build_skill_evidence(required_skills=["SQL"])
        add_job_extraction_skill_evidence(session, extraction_v2.id, merged2)

    with session_scope(session_factory) as session:
        # the ORIGINAL row (from extraction_v1) still exists, unmodified
        original = session.get(JobExtractionSkillEvidence, v1_row_id)
        assert original is not None
        assert original.job_extraction_id == extraction_v1.id
        assert original.skill == "Python"
        assert session.query(JobExtractionSkillEvidence).count() == 2  # both versions' rows coexist


# --- F: transaction rollback (no special machinery -- reuses session_scope) --


def test_raw_skill_evidence_replacement_rolls_back_with_original_intact():
    session_factory = init_db(_in_memory_settings())
    fixed_url = "https://www.naukri.com/job-listings-rollback-test-900002"

    # 1. existing raw evidence exists
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job(url=fixed_url))
        job_id = job.id
        replace_raw_skill_evidence(session, job_id, ld_json_skills=["Python", "SQL"])

    with session_scope(session_factory) as session:
        rows = session.query(JobRawSkillEvidence).filter_by(job_id=job_id).all()
        assert {r.skill_text for r in rows} == {"Python", "SQL"}

    # 2/3. replacement starts, then the SAME transaction is forced to
    # fail via a genuine, PRE-EXISTING constraint (Job.url is already
    # unique=True) -- no bespoke fault-injection machinery added.
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            replace_raw_skill_evidence(
                session, job_id, ld_json_skills=["AWS", "Azure", "Databricks"]
            )
            session.add(Job(
                external_id="rollback-test-duplicate",
                url=fixed_url,  # duplicate -> violates the existing UniqueConstraint
                content_fingerprint="different-fingerprint",
                title="Duplicate URL Job", company="X", location="Y", description="d",
            ))
            session.flush()  # forces the constraint violation now, inside this transaction

    # 4/5. transaction rolled back -> the ORIGINAL raw evidence remains
    # intact; the attempted replacement never became durable.
    with session_scope(session_factory) as session:
        rows = session.query(JobRawSkillEvidence).filter_by(job_id=job_id).all()
        assert {r.skill_text for r in rows} == {"Python", "SQL"}


# --- contributing_sources / conflict / empty-defaults persistence -----


def test_contributing_sources_persisted():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        extraction = add_job_extraction(session, job.id, JobExtractionCreate(required_skills=["Python"]))
        _raw, merged = build_skill_evidence(
            required_skills=["Python"], ld_json_skills=["Python"],
        )
        add_job_extraction_skill_evidence(session, extraction.id, merged)
        row = session.query(JobExtractionSkillEvidence).one()
        assert set(row.contributing_sources) == {"llm", "ld_json"}


def test_classification_conflict_and_conflicting_classifications_persisted():
    """DOM no longer produces classified claims (Run 20 empirical
    validation), so the conflict pair here is deterministic-marker vs
    LLM -- still a genuine, auditable disagreement."""
    from naukri_agent.jobs.skill_evidence import (
        TIER_DETERMINISTIC_MARKER,
        TIER_LLM,
        SkillEvidence,
        merge_skill_evidence,
    )

    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        extraction = add_job_extraction(session, job.id, JobExtractionCreate())
        merged = merge_skill_evidence([
            SkillEvidence("MLOps", "required", "llm", TIER_LLM),
            SkillEvidence(
                "MLOps", "preferred", "deterministic_vocabulary", TIER_DETERMINISTIC_MARKER
            ),
        ])
        add_job_extraction_skill_evidence(session, extraction.id, merged)
        row = session.query(JobExtractionSkillEvidence).one()
        assert row.classification == "preferred"
        assert row.classification_conflict is True
        assert row.conflicting_classifications == [["required", "llm"]]


def test_empty_json_list_defaults_when_nothing_conflicts():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        extraction = add_job_extraction(session, job.id, JobExtractionCreate(required_skills=["Python"]))
        _raw, merged = build_skill_evidence(required_skills=["Python"])
        add_job_extraction_skill_evidence(session, extraction.id, merged)
        row = session.query(JobExtractionSkillEvidence).one()
        assert row.classification_conflict is False
        assert row.conflicting_classifications == []
        assert row.contributing_sources == ["llm"]


# --- D: existing JobExtraction.required_skills/preferred_skills compat -


def test_existing_required_skills_field_unaffected_when_no_hybrid_sources():
    """With no ld_json/key_skills_dom/description input, the merged
    projection must reproduce EXACTLY what was passed in -- proving
    add_job_extraction()'s existing column semantics are untouched."""
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        _raw, merged = build_skill_evidence(
            required_skills=["Python", "SQL"], preferred_skills=["AWS"]
        )
        required_out, preferred_out = merged_required_preferred(merged)
        extraction = add_job_extraction(
            session, job.id,
            JobExtractionCreate(required_skills=required_out, preferred_skills=preferred_out),
        )
        assert sorted(extraction.required_skills) == ["Python", "SQL"]
        assert extraction.preferred_skills == ["AWS"]


def test_existing_preferred_skills_field_still_a_plain_json_list():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        extraction = add_job_extraction(
            session, job.id, JobExtractionCreate(preferred_skills=["Docker", "Kubernetes"])
        )
        assert isinstance(extraction.preferred_skills, list)
        assert extraction.preferred_skills == ["Docker", "Kubernetes"]


# --- I: real-data regression (Generac / Origin HR) ----------------------

GENERAC_DESCRIPTION = """Primary Purpose

The Data Scientist Support role provides ongoing operational and analytical support for Agentic AI and Conversational AI solutions, ensuring their stability, accuracy, and effective performance in production environments.

Work Experience

02 years of experience in data science, analytics, or a related analytical role.
Experience with building agents using Microsoft CoPilot, and other solution paths or equivalent
Familiarity with Databricks, Microsoft, AWS AI tech stack
Relational database experience & Experience query databases (ex: SQL, MySql).
Experience with statistical analysis languages (ex: R, Python, SQL).
Experience on using the cloud platform (ex Azure, AWS..)

Knowledge / Skills / Abilities
Foundational knowledge of statistics, probability, and basic machinelearning concepts.
Experience using SQL and at least one programming language such as Python or R for data analysis.
Familiarity with Agentic AI, Chatbots, Operational AI applications
Familiarity with version control tools (e.g., Git) and collaborative development practices."""

CANDIDATE_SKILLS = [
    "Python", "SQL", "R", "Machine Learning", "Deep Learning", "Generative AI",
    "LLM", "RAG", "NLP", "Computer Vision", "PyTorch", "TensorFlow", "Keras",
    "scikit-learn", "Pandas", "NumPy", "Matplotlib", "OpenCV",
    "Hugging Face Transformers", "spaCy", "MySQL", "Git", "FastAPI",
    "BeautifulSoup", "Selenium", "Scrapy",
]

ORIGIN_HR_DESCRIPTION = """Master s or Ph.D. degree in Statistics, Mathematics, Computer Science, Data Science.

Strong coding skills in Python or R, with extensive use of scientific computing libraries.

Proficiency in SQL for data extraction and manipulation from large relational databases.

Experience with deep learning frameworks or NLP / Generative AI techniques.

Track record of deploying models into production environments and tracking them via MLOps pipelines."""


def test_generac_persists_evidence_for_all_expected_skills():
    from naukri_agent.matching.skill_normalizer import normalize_skill

    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job(
            title="Data Scientist", company="Generac Captiva",
            description=GENERAC_DESCRIPTION,
            url="https://www.naukri.com/job-listings-data-scientist-generac-captiva-900003",
        ))
        replace_raw_skill_evidence(
            session, job.id,
            ld_json_skills=["SQL", "Python", "R", "AWS", "Azure", "Databricks", "Agentic AI"],
        )
        raw_rows = session.query(JobRawSkillEvidence).filter_by(job_id=job.id).all()
        ld_json_skills = [r.skill_text for r in raw_rows if r.source == "ld_json"]

        extraction = add_job_extraction(
            session, job.id,
            JobExtractionCreate(
                required_skills=["SQL", "Python", "R"],
                preferred_skills=["Agentic AI", "Chatbots", "Operational AI"],
            ),
        )
        _raw, merged = build_skill_evidence(
            required_skills=["SQL", "Python", "R"],
            preferred_skills=["Agentic AI", "Chatbots", "Operational AI"],
            ld_json_skills=ld_json_skills,
            description=GENERAC_DESCRIPTION,
            candidate_skills=CANDIDATE_SKILLS,
        )
        add_job_extraction_skill_evidence(session, extraction.id, merged)

        persisted_keys = {
            r.skill_key for r in session.query(JobExtractionSkillEvidence)
            .filter_by(job_extraction_id=extraction.id).all()
        }
        expected = [
            "SQL", "Python", "R", "Machine Learning", "MySQL", "Git",
            "AWS", "Azure", "Databricks", "Agentic AI", "Chatbots", "Operational AI",
        ]
        for skill in expected:
            assert normalize_skill(skill) in persisted_keys, f"{skill} not persisted"


def test_origin_hr_persists_merged_evidence_and_conflict_information():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job(
            title="Data Scientist", company="Origin Hr",
            description=ORIGIN_HR_DESCRIPTION,
            url="https://www.naukri.com/job-listings-data-scientist-origin-hr-900004",
        ))
        extraction = add_job_extraction(
            session, job.id,
            JobExtractionCreate(
                required_skills=["Python", "R", "SQL", "Deep Learning", "NLP", "Generative AI"],
                preferred_skills=["MLOps"],
            ),
        )
        _raw, merged = build_skill_evidence(
            required_skills=["Python", "R", "SQL", "Deep Learning", "NLP", "Generative AI"],
            preferred_skills=["MLOps"],
            description=ORIGIN_HR_DESCRIPTION,
            candidate_skills=CANDIDATE_SKILLS,
        )
        add_job_extraction_skill_evidence(session, extraction.id, merged)

        rows = {
            r.skill_key: r for r in session.query(JobExtractionSkillEvidence)
            .filter_by(job_extraction_id=extraction.id).all()
        }
        assert rows["sql"].classification == "required"
        assert rows["mlops"].classification == "preferred"
        assert rows["mlops"].classification_conflict is False
        # SQL was independently confirmed by an explicit deterministic
        # marker ("Proficiency in SQL") -- corroboration must be visible
        assert "deterministic_vocabulary" in rows["sql"].contributing_sources
        assert "llm" in rows["sql"].contributing_sources
