"""
Semantic skill matching: the 3-tier hybrid approved 2026-09-12.

STATUS (2026-09-12, after controlled real-Ollama validation): Tier 3
(the LLM semantic layer) is EXPERIMENTAL and DISABLED IN PRODUCTION.
Multi-phase validation against the real, project-configured Ollama
provider (llama3.2:3b) -- including consistency reruns, an expanded
case matrix, and a genuinely unseen generalization test -- found a
reproducible, deterministic false-positive rate on "related but not
equivalent" skill pairs the model was never explicitly taught about
(e.g. it confidently claimed "Redis" is an ABBREVIATION of "MongoDB",
and "Statistical Modeling" is a SYNONYM of "Data Engineering" -- both
well-formed, schema-valid, closed-enum-compliant claims that no
guardrail here is designed to catch, since they aren't hallucinations
or malformed output, just confidently wrong semantic judgments). No
prompt refinement or generation-parameter change tested (temperature=0,
a fixed seed, an explicitly-taught relatedness-vs-equivalence
distinction) closed this gap; refinements fixed only the exact named
examples they were given, without generalizing to new unnamed pairs of
the same shape. Given false positives are more costly than false
negatives for this system, Tier 3 must stay unreachable from
production until a fundamentally different verification approach is
designed and validated -- see resolve_skills_for_job()'s docstring for
exactly how it is kept unreachable today. Tiers 1-2 (deterministic, no
LLM) are unaffected by this and remain the only tiers matching/
scorer.py's production call path ever exercises.

    Tier 1 (existing, UNCHANGED) -- exact match + the existing 15-entry
        alias table (matching/skill_normalizer.py).
    Tier 2 (new, deterministic, zero LLM cost) -- compound/qualifier
        decomposition: bare "/"-joined OR-alternatives, " and "/" & "/
        comma-joined AND-requirements, and a small trailing-qualifier
        strip list ("programming"/"development"/"skills"/"experience").
    Tier 3 (new, one batched LLM call per job) -- only for whatever
        Tiers 1-2 could not resolve. Returns a CLOSED, validated
        relationship classification, never free text. Every LLM claim
        is independently re-verified against the real candidate skill
        list before being trusted; on any failure (network, malformed
        JSON, invalid schema, invalid enum value) this degrades to
        UNCERTAIN per-item -- it NEVER raises and NEVER crashes
        scoring.

Nothing here touches matching/scorer.py, jobs/parser.py, jobs/
skill_evidence.py, or config weights/thresholds. Integration is purely
additive: matching/skill_matcher.py::score_skills() gained one new
OPTIONAL keyword argument (default None); scorer.py's existing call
site needed no change at all, so it stays byte-for-byte untouched.
With no provider passed (today's actual call chain), Tier 3 is simply
never reached -- Tiers 1-2 alone already fix the two concrete gaps in
the design report ("Machine Learning/AI", "Python programming") with
zero LLM cost.

Zero-inference discipline unchanged from skill_normalizer.py: Tier 2
never infers relatedness, only recognizes explicit compound PUNCTUATION
shapes the JD already used. Tier 3 may recognize genuine semantic
relationships, but only DIRECT_MATCH / SYNONYM / ABBREVIATION /
COMPOUND_OR_MATCH ever count as a match -- RELATED_NOT_EQUIVALENT,
NO_MATCH, and UNCERTAIN never do, enforced in code, not by prompt
wording.

Persistence/audit determination (2026-09-12, made before writing any
new table, per the approved directive): NO new table. JobMatch already
persists, per (candidate, job) pair and linked to the exact
job_extraction_id that produced it, a JSON positive_factors/
negative_factors pair that is now tier-distinguishing free text --
Tier 1 stays byte-identical ("<skill> required skill matched"), Tier 2
says which candidate skill actually satisfied it ("... matched via
<candidate_skill>" / "... matched (all parts present)" for AND). That
is sufficient to audit everything this phase actually produces, because:
  1. Tier 2 is pure deterministic reconstruction -- given the same
     JobExtraction + CandidateProfile.skills, resolve_deterministic()
     always returns the same answer, so there is nothing time-varying
     to lose; re-deriving it later for audit is exact, not a guess.
  2. Tier 3 is NOT wired into the live pipeline -- matching/scorer.py's
     one call site never passes llm_provider, so no production
     JobMatch row today was ever produced by an LLM relationship
     classification. Adding a table to audit data that does not yet
     exist would be speculative schema expansion the directive
     explicitly warns against.
If Tier 3 is later wired into the live pipeline, THEN a new,
versioned, per-skill row (relationship enum + explanation + which
tier resolved it + whether re-verification downgraded it) would become
justified, mirroring the existing JobExtractionSkillEvidence pattern
(immutable per extraction version) -- but that is a decision for the
phase that actually activates Tier 3 in production, not this one.
"""

from __future__ import annotations

import enum
import json
import logging
import re
from typing import NamedTuple

from pydantic import BaseModel, Field, ValidationError

from naukri_agent.llm.base import LLMProvider
from naukri_agent.llm.exceptions import LLMError
from naukri_agent.llm.response_parsing import extract_json_text
from naukri_agent.matching.skill_normalizer import find_match, normalize_skill, skills_equal

logger = logging.getLogger(__name__)


# --- Tier 2: deterministic compound / qualifier handling -------------------

# Small, explicit, reviewed -- same discipline as skill_normalizer.py's
# SKILL_ALIASES table. Bare slash-joined pairs that are themselves a
# SINGLE idiomatic technology name, never an OR-alternative list.
IDIOMATIC_SLASH_TERMS = frozenset({"tcp/ip", "ci/cd"})

# Small, explicit trailing-qualifier strip list -- NOT an open-ended
# synonym dictionary. Only a generic trailing noun that adds no skill
# information ("Python programming" means the skill "Python").
QUALIFIER_SUFFIXES = ("programming", "development", "skills", "experience")


def strip_qualifier_suffix(skill: str) -> str:
    """"Python programming"/"Python development"/"Python skills"/
    "Python experience" -> "Python". Returns the input unchanged if no
    trailing qualifier is present."""
    stripped = skill.strip()
    low = stripped.lower()
    for suffix in QUALIFIER_SUFFIXES:
        marker = " " + suffix
        if low.endswith(marker) and len(stripped) > len(marker):
            return stripped[: -len(marker)].strip()
    return stripped


_AND_JOIN_RE = re.compile(r"\s+(?:and|&)\s+", re.IGNORECASE)
_AND_SPLIT_RE = re.compile(r",\s*|\s+(?:and|&)\s+", re.IGNORECASE)


def split_or_compound(skill: str) -> list[str] | None:
    """
    "Machine Learning/AI" -> ["Machine Learning", "AI"]. Returns None
    (not an OR-compound) when: there is no bare slash; the whole phrase
    is a known idiomatic slash term (IDIOMATIC_SLASH_TERMS); or every
    part looks like a short, all-caps acronym fragment (<=3 chars,
    upper-case) -- the same SHAPE as "TCP/IP"/"CI/CD" even if not on
    the explicit list, so an unlisted idiom is still refused rather
    than guessed at (conservative: it falls through to Tier 3 instead
    of being silently mis-split).
    """
    stripped = skill.strip()
    if not stripped or "/" not in stripped:
        return None
    if stripped.lower() in IDIOMATIC_SLASH_TERMS:
        return None
    parts = [p.strip() for p in stripped.split("/")]
    if len(parts) < 2 or any(not p for p in parts):
        return None
    if all(len(p) <= 3 and p.isupper() for p in parts):
        return None
    return parts


def split_and_compound(skill: str) -> list[str] | None:
    """
    "Python and SQL" / "Python & SQL" / "Python, SQL" ->
    ["Python", "SQL"]. Returns None when the string contains neither a
    comma nor an "and"/"&" join -- i.e. is not a mechanical list at all.
    """
    stripped = skill.strip()
    if not stripped:
        return None
    if "," in stripped or _AND_JOIN_RE.search(stripped):
        parts = [p.strip() for p in _AND_SPLIT_RE.split(stripped) if p.strip()]
        if len(parts) >= 2:
            return parts
    return None


class TierResolution(NamedTuple):
    """Outcome of resolving one JD skill against the candidate's skill
    list, at whichever tier settled it (or failed to)."""

    matched: bool
    matched_candidate_skill: str | None
    tier: str  # "tier1" | "tier2_qualifier" | "tier2_or" | "tier2_and" | "tier2_and_incomplete" | "tier3_llm" | "unresolved"
    needs_tier3: bool


def _find_match_with_qualifier_fallback(candidate_phrase: str, candidate_skills: list[str]) -> str | None:
    return find_match(candidate_phrase, candidate_skills) or find_match(
        strip_qualifier_suffix(candidate_phrase), candidate_skills
    )


def resolve_deterministic(skill: str, candidate_skills: list[str]) -> TierResolution:
    """
    Tier 1 then Tier 2, in order. Never calls an LLM. Returns
    needs_tier3=True only when nothing here could resolve it either
    way -- AND-compounds that fail to fully resolve are a deliberate,
    bounded exception: they are treated as a definitive non-match
    here and are NOT forwarded to Tier 3 (resolving one missing AND
    part semantically still wouldn't make the other missing parts
    present -- see the implementation report for this scope boundary).
    """
    # Tier 1: exact/alias, unchanged.
    m = find_match(skill, candidate_skills)
    if m is not None:
        return TierResolution(True, m, "tier1", False)

    # Tier 2a: trailing-qualifier strip.
    stripped = strip_qualifier_suffix(skill)
    if stripped != skill:
        m = find_match(stripped, candidate_skills)
        if m is not None:
            return TierResolution(True, m, "tier2_qualifier", False)

    # Tier 2b: OR-compound -- satisfied by ANY alternative.
    or_parts = split_or_compound(skill)
    if or_parts is not None:
        for part in or_parts:
            m = _find_match_with_qualifier_fallback(part, candidate_skills)
            if m is not None:
                return TierResolution(True, m, "tier2_or", False)
        # No alternative resolved deterministically -- still a genuine
        # OR-compound; let Tier 3 attempt a semantic match on the whole
        # original phrase (it may recognize a synonym the static alias
        # table doesn't have for one of the alternatives).
        return TierResolution(False, None, "unresolved", True)

    # Tier 2c: AND-compound -- satisfied only if ALL parts resolve.
    and_parts = split_and_compound(skill)
    if and_parts is not None:
        matches = [_find_match_with_qualifier_fallback(p, candidate_skills) for p in and_parts]
        if all(matches):
            # first part's match stands in for "the" matched candidate
            # skill in the positive-factor text; all parts are present.
            return TierResolution(True, matches[0], "tier2_and", False)
        return TierResolution(False, None, "tier2_and_incomplete", False)

    # No compound shape recognized at all -- a genuine candidate for
    # semantic (Tier 3) resolution.
    return TierResolution(False, None, "unresolved", True)


# --- Tier 3: batched LLM semantic resolution --------------------------------
#
# EXPERIMENTAL / DISABLED IN PRODUCTION (2026-09-12) -- see the module
# docstring above for the full validation findings. This code is kept
# (not deleted) because it is a real, working, independently-testable
# capability that simply hasn't cleared the reliability bar yet -- not
# because it is scheduled to be wired in soon. Nothing below this line
# is reachable unless a caller explicitly constructs an LLMProvider and
# passes it in; matching/scorer.py's production call path never does.


class SkillRelationship(str, enum.Enum):
    DIRECT_MATCH = "DIRECT_MATCH"
    SYNONYM = "SYNONYM"
    ABBREVIATION = "ABBREVIATION"
    COMPOUND_OR_MATCH = "COMPOUND_OR_MATCH"
    RELATED_NOT_EQUIVALENT = "RELATED_NOT_EQUIVALENT"
    NO_MATCH = "NO_MATCH"
    UNCERTAIN = "UNCERTAIN"


# Only these ever count as a match for scoring purposes. Enforced here,
# never by trusting the LLM's own framing of its answer.
MATCHING_RELATIONSHIPS = frozenset(
    {
        SkillRelationship.DIRECT_MATCH,
        SkillRelationship.SYNONYM,
        SkillRelationship.ABBREVIATION,
        SkillRelationship.COMPOUND_OR_MATCH,
    }
)


class SkillResolutionItem(BaseModel):
    jd_skill: str
    relationship: SkillRelationship
    matched_candidate_skill: str | None = None
    explanation: str = ""


class SemanticResolutionPayload(BaseModel):
    resolutions: list[dict] = Field(default_factory=list)  # validated item-by-item, see resolve_semantic


SEMANTIC_SYSTEM_PROMPT = """You determine whether a candidate's existing skills satisfy specific job-requirement phrases. You do not extract new information and you do not evaluate the candidate as a person -- you only classify a relationship between a JD phrase and a FIXED list of candidate skills.

You are given:
1. A list of JD skill phrases that could not be resolved by exact or alias matching.
2. The candidate's COMPLETE, FIXED list of skills. This list is closed -- you may only ever select a candidate skill VERBATIM from it, character-for-character. Never invent, guess, expand, or paraphrase a candidate skill that is not literally in the provided list.

For EACH JD skill phrase, classify the relationship to the candidate's skill list using exactly one of these values:
- "DIRECT_MATCH": the JD phrase and a candidate skill name the same thing, just phrased differently (not already an obvious alias).
- "SYNONYM": a candidate skill is a well-known synonym of the JD phrase.
- "ABBREVIATION": the JD phrase is an abbreviation/expansion of a candidate skill (or vice versa) not already an obvious alias.
- "COMPOUND_OR_MATCH": the JD phrase names two or more alternatives (e.g. "X/Y", "X or Y") and the candidate has AT LEAST ONE of the named alternatives.
- "RELATED_NOT_EQUIVALENT": the JD phrase names a genuinely different (even if related) skill or technique than what the candidate has -- e.g. Predictive Modeling is NOT the same as Machine Learning; Deep Learning is NOT the same as Machine Learning; Computer Vision is NOT NLP; AWS is NOT Azure; TensorFlow is NOT PyTorch. Use this whenever the two are merely adjacent, not the same thing.
- "NO_MATCH": nothing in the candidate's list is plausibly related at all.
- "UNCERTAIN": you cannot confidently decide.

Be conservative: only use DIRECT_MATCH/SYNONYM/ABBREVIATION/COMPOUND_OR_MATCH when you are genuinely confident the candidate's named skill satisfies the JD phrase. When in doubt, use UNCERTAIN or RELATED_NOT_EQUIVALENT, never guess in the candidate's favor.

Return ONLY a single JSON object of this exact shape, nothing else:
{
  "resolutions": [
    {"jd_skill": "<exact phrase as given>", "relationship": "<one of the values above>", "matched_candidate_skill": "<exact candidate skill string, or null>", "explanation": "<one short sentence>"}
  ]
}
One entry per JD skill phrase given to you, in any order. matched_candidate_skill MUST be null unless relationship is DIRECT_MATCH, SYNONYM, ABBREVIATION, or COMPOUND_OR_MATCH, and MUST then be copied verbatim from the candidate skill list."""


def build_semantic_prompt(unresolved_skills: list[str], candidate_skills: list[str]) -> tuple[str, str]:
    user_prompt = (
        "JD skill phrases needing classification:\n"
        + "\n".join(f"- {s}" for s in unresolved_skills)
        + "\n\nCandidate's complete, fixed skill list (select from these ONLY):\n"
        + "\n".join(f"- {s}" for s in candidate_skills)
        + "\n\nReturn only the JSON object described in the system instructions."
    )
    return SEMANTIC_SYSTEM_PROMPT, user_prompt


def _verify_item(item: SkillResolutionItem, jd_skill: str, candidate_skills: list[str]) -> SkillResolutionItem:
    """
    Deterministic re-verification -- never trust the LLM's own claim.
    Returns a (possibly downgraded) item; never raises.
    """
    if item.jd_skill != jd_skill:
        # Defensive: shouldn't happen since callers key by the exact
        # requested jd_skill already, but never trust echoed text either.
        item = item.model_copy(update={"jd_skill": jd_skill})

    if item.relationship not in MATCHING_RELATIONSHIPS:
        return item.model_copy(update={"matched_candidate_skill": None})

    claimed = item.matched_candidate_skill
    if not claimed:
        return item.model_copy(update={"relationship": SkillRelationship.UNCERTAIN, "matched_candidate_skill": None})

    # Hallucination guard: the claimed candidate skill must be a REAL,
    # verbatim (case/whitespace-normalized) entry in the candidate's
    # own list -- never trust the LLM's spelling of it.
    verified = find_match(claimed, candidate_skills)
    if verified is None:
        return item.model_copy(update={"relationship": SkillRelationship.UNCERTAIN, "matched_candidate_skill": None})

    if item.relationship == SkillRelationship.COMPOUND_OR_MATCH:
        # The claimed OR-branch must actually occur in the JD phrase --
        # never allow the LLM to invent an alternative that isn't there.
        or_parts = split_or_compound(jd_skill) or [jd_skill]
        if not any(skills_equal(verified, part) or normalize_skill(part) in normalize_skill(verified) for part in or_parts):
            return item.model_copy(update={"relationship": SkillRelationship.UNCERTAIN, "matched_candidate_skill": None})

    return item.model_copy(update={"matched_candidate_skill": verified})


def resolve_semantic(
    unresolved_skills: list[str],
    candidate_skills: list[str],
    provider: LLMProvider,
) -> dict[str, SkillResolutionItem]:
    """
    ONE batched call covering every unresolved skill for a job. Never
    raises: any failure (LLMError, invalid JSON, invalid top-level
    shape) degrades EVERY requested skill to UNCERTAIN. An individual
    malformed item inside an otherwise-valid response degrades only
    that one item to UNCERTAIN, not the whole batch.
    """

    def _conservative_fallback(reason: str) -> dict[str, SkillResolutionItem]:
        logger.warning("resolve_semantic: %s -- degrading %d skill(s) to UNCERTAIN", reason, len(unresolved_skills))
        return {
            s: SkillResolutionItem(jd_skill=s, relationship=SkillRelationship.UNCERTAIN, explanation=reason)
            for s in unresolved_skills
        }

    if not unresolved_skills:
        return {}

    system, user = build_semantic_prompt(unresolved_skills, candidate_skills)
    try:
        raw_response = provider.complete(system, user, json_mode=True)
    except LLMError as exc:
        return _conservative_fallback(f"llm_error: {exc}")

    try:
        json_text = extract_json_text(raw_response)
        data = json.loads(json_text)
    except (json.JSONDecodeError, ValueError) as exc:
        return _conservative_fallback(f"invalid_json: {exc}")

    if not isinstance(data, dict) or not isinstance(data.get("resolutions"), list):
        return _conservative_fallback("invalid_shape: missing 'resolutions' list")

    requested = set(unresolved_skills)
    results: dict[str, SkillResolutionItem] = {}
    for raw_item in data["resolutions"]:
        if not isinstance(raw_item, dict):
            continue
        jd_skill = raw_item.get("jd_skill")
        if not isinstance(jd_skill, str) or jd_skill not in requested or jd_skill in results:
            continue  # ignore hallucinated/duplicate/unrequested entries, never crash on them
        try:
            item = SkillResolutionItem.model_validate(raw_item)
        except ValidationError as exc:
            logger.warning("resolve_semantic: invalid item for %r (%s) -- UNCERTAIN", jd_skill, exc)
            results[jd_skill] = SkillResolutionItem(
                jd_skill=jd_skill, relationship=SkillRelationship.UNCERTAIN, explanation="invalid_llm_item"
            )
            continue
        results[jd_skill] = _verify_item(item, jd_skill, candidate_skills)

    # Any requested skill the model never answered at all -> conservative.
    for s in unresolved_skills:
        if s not in results:
            results[s] = SkillResolutionItem(
                jd_skill=s, relationship=SkillRelationship.UNCERTAIN, explanation="no_llm_response_for_skill"
            )
    return results


def is_effective_match(item: SkillResolutionItem) -> bool:
    return item.relationship in MATCHING_RELATIONSHIPS and item.matched_candidate_skill is not None


# --- Orchestration: one batched Tier-3 call per job -------------------------


def resolve_skills_for_job(
    required_skills: list[str],
    preferred_skills: list[str],
    candidate_skills: list[str],
    llm_provider: LLMProvider | None = None,
) -> dict[tuple[str, str], TierResolution]:
    """
    Resolve every required+preferred skill for ONE job. Tiers 1-2 run
    for everything, cost-free. If anything remains unresolved AND an
    llm_provider was supplied, exactly ONE batched Tier-3 call is made
    covering every distinct unresolved skill string across BOTH lists
    -- never one call per skill, never one call per list.

    llm_provider=None (today's actual call chain via matching/scorer.py,
    which passes nothing) simply skips Tier 3 -- the SAME conservative,
    never-crash behavior as an LLM failure. Keyed by (label, skill) so
    the same literal skill text appearing in both required and
    preferred lists is resolved independently for each.

    Tier 3 is EXPERIMENTAL / DISABLED IN PRODUCTION (see module
    docstring) -- matching/scorer.py's score_skills() call site passes
    only 4 positional arguments and never supplies llm_provider, so
    this function always receives None there and Tier 3 is never
    reached in the real pipeline. It only activates if some future
    caller explicitly constructs and passes an LLMProvider, e.g. from a
    test or an ad-hoc validation script -- not a code path production
    takes today.
    """
    results: dict[tuple[str, str], TierResolution] = {}
    unresolved_keys: list[tuple[str, str]] = []

    for label, skills in (("required", required_skills), ("preferred", preferred_skills)):
        for skill in skills:
            resolution = resolve_deterministic(skill, candidate_skills)
            results[(label, skill)] = resolution
            if resolution.needs_tier3:
                unresolved_keys.append((label, skill))

    if unresolved_keys and llm_provider is not None:
        unique_unresolved = list(dict.fromkeys(skill for _, skill in unresolved_keys))
        semantic_results = resolve_semantic(unique_unresolved, candidate_skills, llm_provider)
        for key in unresolved_keys:
            _, skill = key
            item = semantic_results.get(skill)
            if item is not None and is_effective_match(item):
                results[key] = TierResolution(True, item.matched_candidate_skill, "tier3_llm", False)
            # else: leave the existing "unresolved"/not-matched TierResolution as-is

    return results
