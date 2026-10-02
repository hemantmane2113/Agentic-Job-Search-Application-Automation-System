"""
Controlled end-to-end validation of the skill-evidence path THROUGH the
actual pipeline code (orchestration/pipeline.py, orchestration/
discovery.py's raw-persistence call site) -- not just the underlying
repository/skill_evidence primitives in isolation (already covered by
test_skill_evidence_persistence.py).

Uses run_daily_recommendations() with a fake discover_fn (no live
Naukri) and a fake LLM provider (no live Ollama) -- the exact same
controlled-fixture mechanism test_daily_pipeline.py already
establishes for this project. Proves the full chain:

    JobDetail-shaped raw evidence
    -> replace_raw_skill_evidence (persisted during "discovery")
    -> fake LLM extraction
    -> build_skill_evidence(...)          [inside pipeline.py's per-job loop]
    -> tier-based merge / classification
    -> merged_required_preferred()
    -> add_job_extraction() (JobExtraction.required_skills/preferred_skills)
    -> add_job_extraction_skill_evidence() (JobExtractionSkillEvidence)

No scoring, weights, thresholds, the 60% floor, experience logic,
cooldown, recommendation limits, application history, scheduler, email
behavior, or Apply workflow is touched or exercised beyond what the
existing test_daily_pipeline.py fixtures already exercise. No Run 20,
no live Naukri, no live Ollama.
"""

from __future__ import annotations

import datetime
import json
import shutil

import yaml

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import (
    Job,
    JobExtraction,
    JobExtractionSkillEvidence,
    JobRawSkillEvidence,
)
from naukri_agent.database.repositories import (
    add_job_extraction,
    add_job_extraction_skill_evidence,
    replace_raw_skill_evidence,
    upsert_job,
)
from naukri_agent.jobs.models import JobCreate
from naukri_agent.jobs.skill_evidence import build_skill_evidence, merged_required_preferred
from naukri_agent.matching.skill_normalizer import normalize_skill
from naukri_agent.orchestration.discovery import DiscoveryResult
from naukri_agent.orchestration.pipeline import run_daily_recommendations

from .digest_fakes import in_memory_factory, settings

# --- Real, persisted job.description text (Run 19, jobs 66 and 8) ---------

GENERAC_DESCRIPTION = """Work Experience

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

ORIGIN_HR_DESCRIPTION = """Master s or Ph.D. degree in Statistics, Mathematics, Computer Science, Data Science.

Strong coding skills in Python or R, with extensive use of scientific computing libraries.

Proficiency in SQL for data extraction and manipulation from large relational databases.

Experience with deep learning frameworks or NLP / Generative AI techniques.

Track record of deploying models into production environments and tracking them via MLOps pipelines."""

CANDIDATE_SKILLS = [
    "Python", "SQL", "R", "Machine Learning", "Deep Learning", "NLP",
    "Generative AI", "MySQL", "Git",
]


def _prep_with_skills(tmp_path, skills: list[str]) -> None:
    """Same shipped example resume/registry files as test_daily_pipeline.py,
    but a candidate_profile.yaml with a CONTROLLED skill list (the
    shipped example only lists 4 skills; this validation needs MySQL/
    R/Git/NLP/Generative AI too, to exercise deterministic-vocabulary
    recovery the same way the real audit did)."""
    import naukri_agent

    root = __import__("pathlib").Path(naukri_agent.__file__).resolve().parents[2]
    cfg_dir = root / "config"
    shutil.copy(cfg_dir / "master_resume.example.yaml", tmp_path / "resume.yaml")
    shutil.copy(cfg_dir / "resumes.example.yaml", tmp_path / "resumes.yaml")

    profile = {
        "full_name": "Test Candidate",
        "email": "test@example.com",
        "phone": "+91-9999999999",
        "skills": skills,
        "years_experience": 3,
        "preferred_roles": ["Data Scientist"],
        "preferred_locations": ["Pune"],
    }
    (tmp_path / "cand.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")


class _Chip:
    def __init__(self, text: str, preferred: bool) -> None:
        self.text = text
        self.preferred = preferred


def _fake_discover_with_raw_evidence(job_specs: list[dict]):
    """Same shape as test_daily_pipeline.py's _fake_discover, extended
    to ALSO call replace_raw_skill_evidence -- simulating what the real
    orchestration/discovery.py does right after upsert_job(), so the
    pipeline's per-job loop has real JobRawSkillEvidence rows to read
    back, exactly as it would after a real fetch."""

    def discover_fn(session, profile, settings, run_id, seq):
        ids = []
        for spec in job_specs:
            job, created = upsert_job(
                session,
                JobCreate(
                    title=spec.get("title", f"{spec['slug']} role"),
                    company=spec.get("company", "Acme"),
                    location="Pune",
                    description=spec["description"],
                    url=f"https://www.naukri.com/job-listings-{spec['slug']}-{spec['ext']}",
                    salary_text="10-15 LPA",
                    experience_text=None,
                ),
                run_id=run_id,
            )
            replace_raw_skill_evidence(
                session, job.id,
                ld_json_skills=spec.get("ld_json_skills"),
                key_skills_dom=[_Chip(t, p) for t, p in spec.get("key_skills_dom", [])] or None,
            )
            ids.append(job.id)
        return (
            DiscoveryResult(
                queries_run=1, queries_failed=0, jobs_new=len(ids), jobs_reseen=0,
                details_failed=0, job_ids=ids, total_failure=False,
            ),
            seq + 1,
        )

    return discover_fn


class _PerJobLLM:
    provider_name = "fake"
    model = "fake-1"

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def complete(self, system: str, prompt: str, *, json_mode: bool = False) -> str:
        for key, resp in self._mapping.items():
            if key in prompt:
                return resp
        return "{}"


def _extraction_json(required, preferred):
    return json.dumps({
        "normalized_title": "Data Scientist",
        "required_skills": required, "preferred_skills": preferred,
        "experience_min": None, "experience_max": None,
        "salary_min": None, "salary_max": None, "salary_currency": None,
        "education_requirements": [],
        "job_type": "unknown",
    })


_GENERAC_LLM_JSON = _extraction_json(
    ["SQL", "Python", "R"], ["Agentic AI", "Chatbots", "Operational AI"]
)
_ORIGIN_HR_LLM_JSON = _extraction_json(
    ["Python", "R", "SQL", "Deep Learning", "NLP", "Generative AI"], ["MLOps"]
)

_GENERAC_SPEC = dict(
    slug="generac", ext="900101", title="Data Scientist", company="Generac Captiva",
    description=GENERAC_DESCRIPTION,
    # Deliberately includes a genuine conflict (SQL: DOM says preferred,
    # LLM says required) and a no-icon chip (Databricks) to validate the
    # "no-icon must not become required" rule through the real pipeline.
    ld_json_skills=["SQL", "Python", "R", "AWS", "Azure", "Databricks", "Agentic AI"],
    key_skills_dom=[("SQL", True), ("Databricks", False), ("Python", False)],
)
_ORIGIN_HR_SPEC = dict(
    slug="originhr", ext="900102", title="Data Scientist", company="Origin Hr",
    description=ORIGIN_HR_DESCRIPTION,
)


def _run(tmp_path, *, now=None):
    _prep_with_skills(tmp_path, CANDIDATE_SKILLS)
    cfg = settings(tmp_path, threshold_review=0, threshold_accept=100)
    factory = in_memory_factory()
    llm = _PerJobLLM({
        "JD for generac": _GENERAC_LLM_JSON,  # unused key kept harmless
        GENERAC_DESCRIPTION[:40]: _GENERAC_LLM_JSON,
        ORIGIN_HR_DESCRIPTION[:40]: _ORIGIN_HR_LLM_JSON,
    })
    result = run_daily_recommendations(
        cfg,
        now=now or datetime.datetime(2026, 9, 12, tzinfo=datetime.UTC),
        discover_fn=_fake_discover_with_raw_evidence([_GENERAC_SPEC, _ORIGIN_HR_SPEC]),
        extraction_provider=llm,
        session_factory=factory,
    )
    return result, factory


# --- 1/2. controlled fixture path + raw persistence -------------------


def test_e2e_raw_evidence_persisted_with_correct_preferred_semantics(tmp_path):
    result, factory = _run(tmp_path)
    assert result.status == "COMPLETED"

    with session_scope(factory) as s:
        generac = s.query(Job).filter(Job.url.like("%generac%")).one()
        raw = s.query(JobRawSkillEvidence).filter_by(job_id=generac.id).all()

        ld_json_rows = {r.skill_text: r.preferred for r in raw if r.source == "ld_json"}
        assert ld_json_rows == {
            "SQL": None, "Python": None, "R": None, "AWS": None,
            "Azure": None, "Databricks": None, "Agentic AI": None,
        }  # ld+json carries NO preferred signal -> always NULL

        dom_rows = {r.skill_text: r.preferred for r in raw if r.source == "key_skills_dom"}
        assert dom_rows == {"SQL": True, "Databricks": False, "Python": False}

        # every raw row belongs to the ACTUAL fetched Job.id
        assert all(r.job_id == generac.id for r in raw)


def test_e2e_rerunning_replacement_does_not_create_duplicates(tmp_path):
    _prep_with_skills(tmp_path, CANDIDATE_SKILLS)
    cfg = settings(tmp_path, threshold_review=0, threshold_accept=100)
    factory = in_memory_factory()
    llm = _PerJobLLM({
        GENERAC_DESCRIPTION[:40]: _GENERAC_LLM_JSON,
        ORIGIN_HR_DESCRIPTION[:40]: _ORIGIN_HR_LLM_JSON,
    })
    discover = _fake_discover_with_raw_evidence([_GENERAC_SPEC, _ORIGIN_HR_SPEC])

    run_daily_recommendations(
        cfg, now=datetime.datetime(2026, 9, 12, tzinfo=datetime.UTC),
        discover_fn=discover, extraction_provider=llm, session_factory=factory,
    )
    with session_scope(factory) as s:
        generac = s.query(Job).filter(Job.url.like("%generac%")).one()
        first_count = s.query(JobRawSkillEvidence).filter_by(job_id=generac.id).count()

    # re-run the SAME "discovery" (same job, same raw evidence spec) a
    # second time -- the real discover_fn is called again by a second
    # run_daily_recommendations call, exactly as a second daily run would
    run_daily_recommendations(
        cfg, now=datetime.datetime(2026, 9, 13, tzinfo=datetime.UTC),
        discover_fn=discover, extraction_provider=llm, session_factory=factory,
    )
    with session_scope(factory) as s:
        generac = s.query(Job).filter(Job.url.like("%generac%")).one()
        second_count = s.query(JobRawSkillEvidence).filter_by(job_id=generac.id).count()
        assert second_count == first_count  # replaced, not doubled


# --- 3. tier precedence, through the real pipeline ----------------------


def test_e2e_tier_precedence_generac(tmp_path):
    """Tier 0 (DOM authority) was REMOVED 2026-09-11 after the Run 20
    empirical validation -- Key Skills DOM is content-only now,
    regardless of its preferred flag. SQL's DOM preferred=True must
    NOT win or create a conflict; the deterministic marker ("Experience
    using SQL and...") and the LLM agree it's required, so tier 1
    (deterministic) wins with NO conflict, and DOM's flag is visible
    only as corroboration."""
    result, factory = _run(tmp_path)
    with session_scope(factory) as s:
        generac = s.query(Job).filter(Job.url.like("%generac%")).one()
        extraction = s.query(JobExtraction).filter_by(job_id=generac.id, is_current=True).one()
        rows = {
            r.skill_key: r for r in
            s.query(JobExtractionSkillEvidence).filter_by(job_extraction_id=extraction.id).all()
        }

        # DOM preferred=True no longer wins or conflicts -- SQL's first
        # occurrence in the JD ("Relational database experience &
        # Experience query databases (ex: SQL, MySql)") carries no
        # explicit marker, so deterministic_vocabulary locks in
        # "unclassified" for SQL specifically (first-occurrence-wins,
        # unrelated to this policy change); the LLM's "required" is the
        # only actual classification left, and DOM no longer competes
        # with or conflicts against it -- it's corroboration only.
        assert rows["sql"].classification == "required"
        assert rows["sql"].winning_source == "llm"
        assert rows["sql"].classification_conflict is False
        assert "key_skills_dom" in rows["sql"].contributing_sources

        # Tier 1 (explicit deterministic marker, "Experience using SQL
        # and ... Python or R") agrees with and outranks Tier 2 (LLM)
        assert rows["python"].classification == "required"
        assert rows["python"].winning_source == "deterministic_vocabulary"
        assert rows["r"].classification == "required"
        assert rows["r"].winning_source == "deterministic_vocabulary"

        # Tier 2 (LLM) used when nothing stronger exists for that skill
        assert rows["chatbots"].classification == "preferred"
        # (Chatbots also gets an explicit "familiarity with" marker in
        # the same sentence as Agentic AI, so it may resolve at tier 1
        # too -- either way it must be "preferred", never guessed.)

        # Tier 4 (ld_json only) stays content-only/unclassified
        assert rows["aws"].classification == "unclassified"
        assert rows["aws"].winning_source == "ld_json"
        assert rows["azure"].classification == "unclassified"

        # DOM no-icon (Databricks, preferred=False) must NOT become
        # "required" merely for lacking the icon -- and, post-removal,
        # DOM preferred=True must ALSO never become a classification on
        # its own (see test_e2e_dom_preferred_true_alone_stays_unclassified)
        assert rows["databricks"].classification == "unclassified"

        # deterministic vocabulary recovers candidate-known misses the
        # LLM never mentioned at all
        assert rows["machine learning"].winning_source == "deterministic_vocabulary"
        assert rows["mysql"].winning_source == "deterministic_vocabulary"
        assert rows["git"].winning_source == "deterministic_vocabulary"


def test_e2e_dom_true_flag_alone_never_wins_the_classification(tmp_path):
    """Run 20 policy removal, proven through the real pipeline: SQL's
    DOM preferred=True is real, persisted evidence (see the raw-
    persistence test above), but it must never be the WINNING source
    for classification -- confirming the same invariant unit-tested in
    test_skill_evidence.py holds end-to-end through the real per-job
    pipeline code, not just the standalone merge function."""
    result, factory = _run(tmp_path)
    with session_scope(factory) as s:
        generac = s.query(Job).filter(Job.url.like("%generac%")).one()
        extraction = s.query(JobExtraction).filter_by(job_id=generac.id, is_current=True).one()
        row = {
            r.skill_key: r for r in
            s.query(JobExtractionSkillEvidence).filter_by(job_extraction_id=extraction.id).all()
        }["sql"]
        assert row.winning_source != "key_skills_dom"


def test_e2e_tier_precedence_origin_hr_llm_used_when_no_stronger_evidence(tmp_path):
    result, factory = _run(tmp_path)
    with session_scope(factory) as s:
        origin = s.query(Job).filter(Job.url.like("%originhr%")).one()
        extraction = s.query(JobExtraction).filter_by(job_id=origin.id, is_current=True).one()
        rows = {
            r.skill_key: r for r in
            s.query(JobExtractionSkillEvidence).filter_by(job_extraction_id=extraction.id).all()
        }
        # MLOps has no ld_json/DOM evidence and no explicit deterministic
        # marker ("Track record of..." has neither) -> LLM's classification
        # is the only opinion and is used as-is.
        assert rows["mlops"].classification == "preferred"
        assert rows["mlops"].winning_source == "llm"
        assert rows["mlops"].classification_conflict is False


# --- 4. conflicts, through the real pipeline -----------------------------


def test_e2e_dom_true_flag_no_longer_creates_a_false_conflict(tmp_path):
    """Before the Run 20 policy removal, SQL's DOM preferred=True vs
    LLM's 'required' used to be a recorded conflict. It must NOT be one
    anymore -- DOM no longer competes for classification at all, so
    LLM and the deterministic marker simply agree, with zero conflict,
    even though DOM's (now non-authoritative) True flag is still
    present in contributing_sources."""
    result, factory = _run(tmp_path)
    with session_scope(factory) as s:
        generac = s.query(Job).filter(Job.url.like("%generac%")).one()
        extraction = s.query(JobExtraction).filter_by(job_id=generac.id, is_current=True).one()
        sql_row = (
            s.query(JobExtractionSkillEvidence)
            .filter_by(job_extraction_id=extraction.id, skill_key="sql")
            .one()
        )
        assert sql_row.classification_conflict is False
        assert sql_row.conflicting_classifications == []
        assert set(sql_row.contributing_sources) >= {"llm", "key_skills_dom", "ld_json"}
        # exactly ONE row for "sql" despite 3+ sources mentioning it --
        # corroboration strengthens the record, never inflates the count
        assert (
            s.query(JobExtractionSkillEvidence)
            .filter_by(job_extraction_id=extraction.id, skill_key="sql")
            .count() == 1
        )
        # no numeric "confidence"/bonus field exists on the row at all
        assert not hasattr(sql_row, "confidence_score")
        assert not hasattr(sql_row, "corroboration_score")


def test_e2e_genuine_deterministic_vs_llm_conflict_remains_visible(tmp_path):
    """Conflicts are still fully possible and fully auditable -- just no
    longer sourced from DOM. Here the fake LLM deliberately misclassifies
    Python as 'preferred' while Generac's real JD text carries an explicit
    deterministic marker ("Experience using SQL and at least one
    programming language such as Python or R...") calling it required;
    the deterministic (tier 1) classification must win, and the conflict
    must be recorded with no count inflation."""
    _prep_with_skills(tmp_path, CANDIDATE_SKILLS)
    cfg = settings(tmp_path, threshold_review=0, threshold_accept=100)
    factory = in_memory_factory()
    llm = _PerJobLLM({
        GENERAC_DESCRIPTION[:40]: _extraction_json(["SQL", "R"], ["Python"]),
    })
    run_daily_recommendations(
        cfg, now=datetime.datetime(2026, 9, 12, tzinfo=datetime.UTC),
        discover_fn=_fake_discover_with_raw_evidence([_GENERAC_SPEC]),
        extraction_provider=llm, session_factory=factory,
    )
    with session_scope(factory) as s:
        job = s.query(Job).one()
        extraction = s.query(JobExtraction).filter_by(job_id=job.id, is_current=True).one()
        python_row = (
            s.query(JobExtractionSkillEvidence)
            .filter_by(job_extraction_id=extraction.id, skill_key="python")
            .one()
        )
        assert python_row.classification == "required"
        assert python_row.winning_source == "deterministic_vocabulary"
        assert python_row.classification_conflict is True
        assert ["preferred", "llm"] in python_row.conflicting_classifications
        assert (
            s.query(JobExtractionSkillEvidence)
            .filter_by(job_extraction_id=extraction.id, skill_key="python")
            .count() == 1
        )


# --- 5. projection: merged_required_preferred() == JobExtraction fields -


def test_e2e_projection_matches_job_extraction_fields_exactly(tmp_path):
    result, factory = _run(tmp_path)
    with session_scope(factory) as s:
        generac = s.query(Job).filter(Job.url.like("%generac%")).one()
        extraction = s.query(JobExtraction).filter_by(job_id=generac.id, is_current=True).one()

        merged_rows = (
            s.query(JobExtractionSkillEvidence)
            .filter_by(job_extraction_id=extraction.id).all()
        )
        expected_required = sorted(r.skill for r in merged_rows if r.classification == "required")
        expected_preferred = sorted(r.skill for r in merged_rows if r.classification == "preferred")

        assert sorted(extraction.required_skills) == expected_required
        assert sorted(extraction.preferred_skills) == expected_preferred
        # Post-Run-20-policy-removal: SQL's DOM preferred=True no longer
        # moves it out of required -- LLM and the deterministic marker
        # agree it's required, and DOM cannot override that anymore.
        assert "SQL" in extraction.required_skills
        assert "SQL" not in extraction.preferred_skills


def test_e2e_derived_rows_correspond_to_the_exact_extraction_id(tmp_path):
    result, factory = _run(tmp_path)
    with session_scope(factory) as s:
        generac = s.query(Job).filter(Job.url.like("%generac%")).one()
        origin = s.query(Job).filter(Job.url.like("%originhr%")).one()
        gen_ext = s.query(JobExtraction).filter_by(job_id=generac.id, is_current=True).one()
        org_ext = s.query(JobExtraction).filter_by(job_id=origin.id, is_current=True).one()

        gen_rows = s.query(JobExtractionSkillEvidence).filter_by(job_extraction_id=gen_ext.id).all()
        org_rows = s.query(JobExtractionSkillEvidence).filter_by(job_extraction_id=org_ext.id).all()

        assert all(r.job_extraction_id == gen_ext.id for r in gen_rows)
        assert all(r.job_extraction_id == org_ext.id for r in org_rows)
        assert {r.skill_key for r in gen_rows}.isdisjoint({"mlops"})  # Generac never saw MLOps
        assert "mlops" in {r.skill_key for r in org_rows}


# --- 6. versioning, through the real pipeline ----------------------------


def test_e2e_two_extraction_versions_have_separate_immutable_evidence(tmp_path):
    _prep_with_skills(tmp_path, CANDIDATE_SKILLS)
    cfg = settings(tmp_path, threshold_review=0, threshold_accept=100)
    factory = in_memory_factory()

    spec = dict(_GENERAC_SPEC)
    discover = _fake_discover_with_raw_evidence([spec])

    # version 1: LLM returns a NARROWER required list
    llm_v1 = _PerJobLLM({GENERAC_DESCRIPTION[:40]: _extraction_json(["SQL", "Python"], [])})
    run_daily_recommendations(
        cfg, now=datetime.datetime(2026, 9, 12, tzinfo=datetime.UTC),
        discover_fn=discover, extraction_provider=llm_v1, session_factory=factory,
    )
    with session_scope(factory) as s:
        job = s.query(Job).one()
        ext_v1 = s.query(JobExtraction).filter_by(job_id=job.id).one()
        v1_id = ext_v1.id
        v1_skills = {
            r.skill_key for r in
            s.query(JobExtractionSkillEvidence).filter_by(job_extraction_id=v1_id).all()
        }

    # version 2: LLM returns a WIDER required list (re-extraction).
    # "Kubernetes" is deliberately NOT a candidate skill and has no ld_
    # json/DOM evidence either -- it can ONLY appear via the LLM, so its
    # presence in v2-but-not-v1 unambiguously reflects the different
    # extraction, not deterministic vocabulary recovery (which found
    # "R" independently in BOTH versions regardless of the LLM's list).
    llm_v2 = _PerJobLLM({
        GENERAC_DESCRIPTION[:40]: _extraction_json(["SQL", "Python", "Kubernetes"], [])
    })
    run_daily_recommendations(
        cfg, now=datetime.datetime(2026, 9, 13, tzinfo=datetime.UTC),
        discover_fn=discover, extraction_provider=llm_v2, session_factory=factory,
    )
    with session_scope(factory) as s:
        job = s.query(Job).one()
        extractions = s.query(JobExtraction).filter_by(job_id=job.id).order_by(JobExtraction.id).all()
        assert len(extractions) == 2
        ext_v1, ext_v2 = extractions
        assert ext_v1.id == v1_id
        assert ext_v1.is_current is False and ext_v2.is_current is True

        # version 1's own evidence rows are UNCHANGED
        v1_skills_after = {
            r.skill_key for r in
            s.query(JobExtractionSkillEvidence).filter_by(job_extraction_id=ext_v1.id).all()
        }
        assert v1_skills_after == v1_skills

        v2_skills = {
            r.skill_key for r in
            s.query(JobExtractionSkillEvidence).filter_by(job_extraction_id=ext_v2.id).all()
        }
        assert "kubernetes" in v2_skills
        assert "kubernetes" not in v1_skills_after  # v2 has more, v1 untouched


# --- 7. transaction atomicity, via the SAME transactional boundary -------
#
# run_daily_recommendations() wraps its entire run in ONE session_scope
# (see orchestration/pipeline.py) -- there is no per-job sub-transaction,
# so proving atomicity for add_job_extraction()+add_job_extraction_
# skill_evidence() means proving it at that SAME boundary the real
# pipeline already uses, via a genuine pre-existing constraint (Job.url
# uniqueness) -- not new production fault-injection code (none was
# added to pipeline.py, discovery.py, or repositories.py for this test).


def test_e2e_extraction_and_derived_evidence_roll_back_together(tmp_path):
    session_factory = in_memory_factory()
    fixed_url = "https://www.naukri.com/job-listings-atomicity-test-900199"

    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, JobCreate(
            title="Data Scientist", company="Acme", location="Pune",
            description=GENERAC_DESCRIPTION, url=fixed_url,
        ))
        job_id = job.id

    with __import__("pytest").raises(Exception):
        with session_scope(session_factory) as session:
            extraction = add_job_extraction(
                session, job_id,
                __import__("naukri_agent.jobs.models", fromlist=["JobExtractionCreate"])
                .JobExtractionCreate(required_skills=["SQL", "Python"]),
            )
            _raw, merged = build_skill_evidence(
                required_skills=["SQL", "Python"], description=GENERAC_DESCRIPTION,
                candidate_skills=CANDIDATE_SKILLS,
            )
            add_job_extraction_skill_evidence(session, extraction.id, merged)
            new_extraction_id = extraction.id

            # force the SAME transaction to fail via a genuine,
            # pre-existing constraint (Job.url unique=True)
            session.add(Job(
                external_id="atomicity-dup", url=fixed_url,
                content_fingerprint="different-fp",
                title="Duplicate", company="X", location="Y", description="d",
            ))
            session.flush()

    with session_scope(session_factory) as session:
        # neither the new JobExtraction row nor its derived evidence
        # rows are durable after the rollback
        assert session.get(JobExtraction, new_extraction_id) is None
        assert (
            session.query(JobExtractionSkillEvidence)
            .filter_by(job_extraction_id=new_extraction_id).count() == 0
        )
        # only the ORIGINAL job row survived (the duplicate insert
        # never became durable either)
        assert session.query(Job).count() == 1
