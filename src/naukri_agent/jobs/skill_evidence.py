"""
Skill evidence: an auditable, source-tagged view of what a job
requires, built from up to four independent inputs —

    Naukri ld+json `skills`       (raw, content-only, never classified)
    Naukri Key Skills DOM chips   (raw, Naukri's own preferred tag)
    the LLM's required/preferred lists (derived, interpretive)
    deterministic candidate-vocabulary recovery against the full JD
        body (derived, verbatim-match only)

— and a single deterministic CLAIM-STRENGTH merge policy (2026-09-11
precedence-policy review, finalized) that collapses them into one
record per distinct skill, never double-counting a skill just because
multiple sources mention it, while preserving enough evidence to audit
WHY a skill exists and WHY its classification won.

Precedence is NOT "source X always beats source Y" — it is a 4-tier
CLAIM-STRENGTH hierarchy, because a single source can carry claims of
different strength depending on HOW the claim was derived:

    Tier 1 — deterministic JD marker classification, but ONLY when an
             explicit hard/soft marker was actually found on the
             specific sentence (not section-state alone)
    Tier 2 — the LLM's own classification
    Tier 3 — weak/default inference: deterministic vocabulary's
             section-state-only fallback (no explicit marker)
    Tier 4 — content-only evidence with no classification authority at
             all: ld_json skill membership, AND Naukri Key Skills DOM
             membership regardless of its preferred flag.

A lower tier NUMBER is a STRONGER claim. A tier only wins if its
classification is not "unclassified" — an "unclassified" record never
outranks any actual required/preferred verdict, regardless of tier.

REMOVED 2026-09-11 (Run 20 empirical validation): Key Skills DOM's
`preferred=True` used to be Tier 0 — treated as authored ground truth
that could outrank even the LLM. A 40-observation sample of real Run 20
jobs found this unsupported: 78.3% of `preferred=True` observations
were skills the JD explicitly stated as REQUIRED (0% were explicitly
preferred), and identical/reposted JD text produced different flags
for the same skill. The flag is still READ and PERSISTED exactly as
observed (see JobRawSkillEvidence — untouched by this change) and
still contributes to `contributing_sources`, but it no longer asserts
a required/preferred classification on its own, regardless of True or
False — Key Skills DOM membership is content-only evidence now, same
standing as ld_json.

This module does NOT touch scoring, the 60% required-skill floor, the
LLM prompt, weights, thresholds, or experience logic. Nothing in
matching/scorer.py or orchestration/ calls it yet (see
merged_required_preferred() for the backward-compatible JobExtraction-
shaped view this is designed to eventually feed).

Zero-inference discipline (same as matching/skill_normalizer.py):
deterministic vocabulary recovery only ever reports a skill the
CANDIDATE already lists, found as a literal (case/whitespace/hyphen/
glued-compound-normalized, word-boundary-aware) substring of the JD
text — never a related or implied skill. "Python appears in the JD"
must never produce evidence for "Django".
"""

from __future__ import annotations

import re
from typing import NamedTuple

from naukri_agent.matching.skill_normalizer import normalize_skill

# --- Claim-strength tiers ----------------------------------------------------
#
# Lower number = stronger claim. See the module docstring for the full
# rationale (2026-09-11 precedence-policy review, revised same day
# after the Run 20 empirical validation removed Tier 0). These are used
# directly as the merge's sort key — there is no separate per-source
# precedence table anymore, because claim strength depends on HOW a
# classification was derived, not merely which of the four inputs it
# came from (a single source can produce claims at different tiers).
TIER_DETERMINISTIC_MARKER = 1
TIER_LLM = 2
TIER_WEAK_DEFAULT = 3
TIER_CONTENT_ONLY = 4


# --- Evidence records ---------------------------------------------------------


class SkillEvidence(NamedTuple):
    """
    One claim about one skill, from one source, at one claim-strength
    tier. Multiple records can (and often should) exist for the same
    skill — that IS the auditability; see merge_skill_evidence() for
    the separate step that collapses them into a single counted
    verdict per skill.
    """

    skill: str
    classification: str  # "required" | "preferred" | "unclassified"
    source: str  # "ld_json" | "key_skills_dom" | "llm" | "deterministic_vocabulary"
    tier: int
    source_section: str | None = None
    source_snippet: str | None = None


class MergedSkillEvidence(NamedTuple):
    """
    The single, counted, scoring-facing verdict for one skill, PLUS
    enough evidence to audit why it exists and why its classification
    won — never just the winning source alone.

    contributing_sources: every source (deduped, sorted) that produced
        ANY evidence for this skill, classified or not — corroboration
        information only; never used to inflate the skill count and
        never a scoring input.
    classification_conflict: True when two or more CLASSIFIED (non-
        "unclassified") sources disagreed on required vs. preferred
        for this skill. Never resolved silently — see
        conflicting_classifications.
    conflicting_classifications: the distinct (classification, source)
        pairs that LOST to the winner — empty when there was no
        conflict (including when every classified source agreed).
    """

    skill: str
    classification: str  # the WINNING classification
    source: str  # the WINNING record's source
    contributing_sources: tuple[str, ...]
    classification_conflict: bool
    conflicting_classifications: tuple[tuple[str, str], ...]


_VALID_CLASSIFICATIONS = ("required", "preferred", "unclassified")
_VALID_SOURCES = ("ld_json", "key_skills_dom", "llm", "deterministic_vocabulary")


# --- Building evidence from each raw/derived source -------------------------


def evidence_from_llm(
    required_skills: list[str] | None,
    preferred_skills: list[str] | None,
) -> list[SkillEvidence]:
    """The LLM's own classification — interpretive, tier 2. Kept as its
    own source tag so the merge policy can weigh it without touching
    JobExtraction itself."""
    out: list[SkillEvidence] = []
    for s in required_skills or []:
        if s and s.strip():
            out.append(SkillEvidence(s, "required", "llm", TIER_LLM))
    for s in preferred_skills or []:
        if s and s.strip():
            out.append(SkillEvidence(s, "preferred", "llm", TIER_LLM))
    return out


def evidence_from_ld_json_skills(ld_json_skills: list[str] | None) -> list[SkillEvidence]:
    """
    Naukri's flat `skills` array. Preserved verbatim — no
    classification is available in this shape (see the 2026-09-11
    ld+json investigation: it's the same content as the Key Skills
    chips, just without the preferred/required split), so every entry
    is content-only, tier 4, "unclassified" until/unless another
    source corroborates it.
    """
    if not ld_json_skills:
        return []
    return [
        SkillEvidence(s, "unclassified", "ld_json", TIER_CONTENT_ONLY)
        for s in ld_json_skills
        if s and s.strip()
    ]


def evidence_from_key_skills_dom(chips: "list | None") -> list[SkillEvidence]:
    """
    Naukri's Key Skills chip widget — CONTENT-ONLY evidence, tier 4,
    always "unclassified", regardless of the chip's `preferred` flag.

    REMOVED 2026-09-11 (Run 20 empirical validation): `preferred=True`
    used to assert "preferred" at tier 0 (authored-ground-truth,
    outranking even the LLM). A 40-observation sample of real Run 20
    jobs found this unsupported — 78.3% of `preferred=True`
    observations were skills the JD explicitly stated as REQUIRED, 0%
    were explicitly preferred, and identical/reposted JD text produced
    different flags for the same skill (see the 2026-09-11 empirical
    validation report). The flag is never reinterpreted as "required"
    either — it simply no longer asserts ANY classification on its
    own; a skill mentioned only here stays "unclassified" until/unless
    a stronger source (deterministic marker, LLM, or vocabulary
    fallback) classifies it. This function does NOT touch how the flag
    is READ or PERSISTED (see database.repositories.
    replace_raw_skill_evidence / JobRawSkillEvidence, both unchanged)
    — only its role in classification changed.

    Accepts anything with `.text`/`.preferred` attributes
    (browser.models.KeySkillChip in production; a plain duck-typed
    object in tests) so this module has no import-time dependency on
    browser/.
    """
    if not chips:
        return []
    out: list[SkillEvidence] = []
    for chip in chips:
        text = getattr(chip, "text", None)
        if not text or not text.strip():
            continue
        out.append(SkillEvidence(text, "unclassified", "key_skills_dom", TIER_CONTENT_ONLY))
    return out


# --- Deterministic required/preferred language markers ---------------------
#
# Same discipline and general shape as jobs/parser.py's
# _REQUIREMENT_EXPERIENCE_MARKERS / _SOFT_EXPERIENCE_MARKERS — small,
# explicit, reviewed, no fuzzy/substring inference beyond the literal
# phrase — but a SEPARATE table, because skill-requirement sentences
# use somewhat different phrasing than experience-years sentences (e.g.
# "proficiency in", "experience with" are central here and irrelevant
# there). A sentence with neither marker is "unclassified" — this
# module NEVER guesses required vs. preferred from silence.
_SKILL_REQUIRED_MARKERS = (
    "required", "requirement", "must have", "must-have", "must",
    "mandatory", "proficiency in", "experience with", "experience in",
    "experience using",
)
_SKILL_PREFERRED_MARKERS = (
    "preferred", "familiarity with", "exposure to", "nice to have",
    "nice-to-have", "good to have", "a plus", "bonus", "welcome",
)

# Section headings toggle a running "inside a required section" state —
# mirrors jobs/parser.py's _SECTION_REQUIRED_RE/_SECTION_SOFT_RE
# (duplicated here, not imported, to keep this module independent of
# jobs/parser.py's private internals; both are small and reviewed).
_SECTION_REQUIRED_RE = re.compile(
    r"^(required|requirements?|must[ -]?have|mandatory)\b", re.IGNORECASE
)
_SECTION_SOFT_RE = re.compile(
    r"^(good to have|nice[ -]?to[ -]?have|preferred|optional|bonus)\b", re.IGNORECASE
)
# Best-effort heading detector used ONLY to label SkillEvidence.source_
# section for audit purposes — never for a classification decision.
# Imprecision here (missing or over-matching a heading) cannot change
# whether a skill is required/preferred/unclassified.
_HEADING_LIKE_RE = re.compile(r"^[A-Z][A-Za-z /&]{2,50}$")

# Very small, explicit negation-cue set. Best-effort/conservative: this
# only catches a negation word appearing BEFORE the match on the SAME
# line — it will not catch negation phrased across a sentence boundary
# or an unusual construction. "Where reasonably detectable", not a
# claim of complete negation handling.
_NEGATION_MARKERS = ("not ", "no ", "without ", "excluding ", "except ")


def classify_skill_sentence(sentence: str) -> str:
    """
    Deterministic required/preferred/unclassified verdict for ONE
    sentence, from explicit language markers only — no section-state,
    no guessing. A sentence containing a soft marker is NEVER required
    regardless of anything else (same "soft always wins" rule as the
    experience-conflict parser); a sentence with neither marker is
    "unclassified". This is the tier-1 decision function: callers that
    get a non-"unclassified" result from THIS function may tag it tier
    1 (explicit marker found); a fallback beyond this function (e.g.
    section-state) is a WEAKER, tier-3 claim — see
    recover_vocabulary_skills().
    """
    low = sentence.lower()
    if any(m in low for m in _SKILL_PREFERRED_MARKERS):
        return "preferred"
    if any(m in low for m in _SKILL_REQUIRED_MARKERS):
        return "required"
    return "unclassified"


# --- Deterministic candidate-vocabulary recovery ----------------------------


def _build_skill_pattern(skill: str) -> "re.Pattern[str] | None":
    """
    Build a case-insensitive, whitespace/hyphen/glued-compound-
    tolerant, word-boundary-aware regex for ONE candidate skill.

    Multi-word skills join their words with [\\s-]* (ZERO or more) —
    not the more obvious [\\s-]+ — because the real persisted JD text
    this was built against turned out to glue words together with NO
    separator at all ("basic machinelearning concepts"), alongside
    other identically-glued artefacts on the same page ("welldefined",
    "realworld", "problemsolving") — evidently a scraping/rendering
    side-effect, not just a hyphenation choice. [\\s-]* matches all
    three real shapes: "machine learning", "machine-learning", and
    "machinelearning".

    Very short single-word skills (e.g. "R") get a stricter boundary
    than plain \\b: \\b alone treats "&" as a break too, so \\bR\\b
    would false-positive inside "R&D" — excluded here via a boundary
    that also rejects a directly-adjacent &, /, or -.
    """
    words = [re.escape(w) for w in re.split(r"\s+", skill.strip()) if w]
    if not words:
        return None
    body = r"[\s\-]*".join(words)
    if len(words) == 1 and len(normalize_skill(skill)) <= 2:
        return re.compile(rf"(?<![\w&/-]){body}(?![\w&/-])", re.IGNORECASE)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


def _is_negated(line_lower: str, match_start: int) -> bool:
    prefix = line_lower[:match_start]
    return any(marker in prefix for marker in _NEGATION_MARKERS)


def recover_vocabulary_skills(
    description: str | None,
    candidate_skills: list[str],
) -> list[SkillEvidence]:
    """
    Scan the FULL raw JD body for literal, verbatim mentions of skills
    the CANDIDATE already lists — never a general technology
    dictionary, never an inference that one skill implies another.
    First non-negated occurrence per skill wins (same "first occurrence
    wins" convention as jobs/parser.py's _cleanup_skill_lists dedupe);
    every occurrence found only under a negation cue is treated as no
    evidence at all for that skill (skill is not recovered).

    Classification/tier per recovered skill:
      - classify_skill_sentence() found an explicit marker on the
        containing line -> that classification, TIER 1 (grounded,
        inspectable, strong).
      - no explicit marker, but the line sits inside a "Required"/
        "Mandatory"-headed section -> "required", TIER 3 (weak
        default — never allowed to override a stronger tier at merge
        time).
      - neither -> "unclassified", TIER_CONTENT_ONLY. Never guessed.

    NEVER touches matching/skill_normalizer.py — search-time hyphen/
    glued-compound tolerance is built locally in _build_skill_pattern
    so the live scoring path this phase must not touch stays byte-
    identical.
    """
    if not description or not candidate_skills:
        return []

    patterns: dict[str, tuple[str, "re.Pattern[str]"]] = {}
    for skill in candidate_skills:
        if not skill or not skill.strip():
            continue
        key = normalize_skill(skill)
        if key in patterns:
            continue  # first candidate spelling for a given normalized skill wins
        pattern = _build_skill_pattern(skill)
        if pattern is not None:
            patterns[key] = (skill, pattern)

    found: dict[str, SkillEvidence] = {}
    in_required_section = False
    current_section: str | None = None

    for raw_line in description.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        low = stripped.lower()

        if _HEADING_LIKE_RE.match(stripped):
            current_section = stripped
        if _SECTION_REQUIRED_RE.match(low):
            in_required_section = True
        elif _SECTION_SOFT_RE.match(low):
            in_required_section = False

        for key, (original_skill, pattern) in patterns.items():
            if key in found:
                continue
            m = pattern.search(stripped)
            if not m:
                continue
            if _is_negated(low, m.start()):
                continue  # negated occurrence -> no evidence; a later genuine mention can still recover it

            explicit = classify_skill_sentence(stripped)
            if explicit != "unclassified":
                classification, tier = explicit, TIER_DETERMINISTIC_MARKER
            elif in_required_section:
                classification, tier = "required", TIER_WEAK_DEFAULT
            else:
                classification, tier = "unclassified", TIER_CONTENT_ONLY

            found[key] = SkillEvidence(
                skill=original_skill,
                classification=classification,
                source="deterministic_vocabulary",
                tier=tier,
                source_section=current_section,
                source_snippet=stripped,
            )

    return list(found.values())


# --- Merge: claim-strength precedence, conflict-preserving -----------------


def merge_skill_evidence(evidence: list[SkillEvidence]) -> list[MergedSkillEvidence]:
    """
    Collapse potentially-many per-source evidence records into exactly
    ONE MergedSkillEvidence per distinct (normalize_skill-equal) skill
    — multiple sources strengthen confidence/auditability, they never
    increase the counted skill total.

    Skill INCLUSION is a union of every source, classified or not — a
    skill is never dropped merely because it has no classification
    (content and classification are separate concepts; see the module
    docstring).

    CLASSIFICATION is resolved by claim strength (lowest `tier` wins)
    among only the CLASSIFIED (non-"unclassified") records for that
    skill; ties broken deterministically by evidence order (first
    occurrence at the winning tier). When 2+ classified records
    disagree on classification, the result is marked
    classification_conflict=True and every losing (classification,
    source) pair is preserved in conflicting_classifications — this is
    NEVER resolved silently.
    """
    by_skill: dict[str, list[SkillEvidence]] = {}
    order: list[str] = []
    for e in evidence:
        if not e.skill or not e.skill.strip():
            continue
        key = normalize_skill(e.skill)
        if key not in by_skill:
            by_skill[key] = []
            order.append(key)
        by_skill[key].append(e)

    merged: list[MergedSkillEvidence] = []
    for key in order:
        records = by_skill[key]
        contributing = tuple(sorted({r.source for r in records}))
        classified = [r for r in records if r.classification != "unclassified"]

        if not classified:
            winner = records[0]
            merged.append(
                MergedSkillEvidence(
                    skill=winner.skill,
                    classification="unclassified",
                    source=winner.source,
                    contributing_sources=contributing,
                    classification_conflict=False,
                    conflicting_classifications=(),
                )
            )
            continue

        winner = min(classified, key=lambda r: r.tier)  # min() is stable -> deterministic tie-break
        losing = tuple(
            sorted(
                {
                    (r.classification, r.source)
                    for r in classified
                    if r.classification != winner.classification
                }
            )
        )
        merged.append(
            MergedSkillEvidence(
                skill=winner.skill,
                classification=winner.classification,
                source=winner.source,
                contributing_sources=contributing,
                classification_conflict=len(losing) > 0,
                conflicting_classifications=losing,
            )
        )
    return merged


def merged_required_preferred(
    merged: list[MergedSkillEvidence],
) -> tuple[list[str], list[str]]:
    """
    Backward-compatible (required_skills, preferred_skills) view of
    the merged evidence — the EXACT shape JobExtraction.required_skills
    / preferred_skills already use (flat list[str], same field names).
    Skills classified "unclassified" are excluded from both lists —
    the existing JobExtraction schema has no third bucket to put them
    in; this is a deliberate scope boundary (see the schema section of
    this phase's report), not an accidental drop of information — the
    full merged/raw evidence remains available separately for audit.
    """
    required = [m.skill for m in merged if m.classification == "required"]
    preferred = [m.skill for m in merged if m.classification == "preferred"]
    return required, preferred


def build_skill_evidence(
    *,
    required_skills: list[str] | None = None,
    preferred_skills: list[str] | None = None,
    ld_json_skills: list[str] | None = None,
    key_skills_dom: "list | None" = None,
    description: str | None = None,
    candidate_skills: list[str] | None = None,
) -> tuple[list[SkillEvidence], list[MergedSkillEvidence]]:
    """
    Single entry point: build evidence from every available source and
    return (raw_evidence, merged_evidence) — raw_evidence is the full,
    un-collapsed audit trail (may contain multiple records per skill,
    one per source); merged_evidence is the one-record-per-skill,
    never-double-counted, conflict-preserving view. Nothing here is
    persisted or read by scoring in this phase — see
    merged_required_preferred() for the backward-compatible
    JobExtraction-shaped projection this is designed to eventually
    feed once the schema question is resolved.
    """
    raw: list[SkillEvidence] = []
    raw.extend(evidence_from_llm(required_skills, preferred_skills))
    raw.extend(evidence_from_ld_json_skills(ld_json_skills))
    raw.extend(evidence_from_key_skills_dom(key_skills_dom))
    raw.extend(recover_vocabulary_skills(description, candidate_skills or []))
    return raw, merge_skill_evidence(raw)
