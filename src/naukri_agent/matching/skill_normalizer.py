"""
Skill normalization.

A small, hand-curated table of genuine equivalences (an abbreviation
or alternate spelling for the SAME skill) — never an inference that
one skill implies or relates to another. "Python" is never linked to
"Django" or "FastAPI" here, because knowing Python does not mean you
know either of those; that kind of inference belongs nowhere in a
scoring system that has to be trusted.

Keep this table small and reviewed. Every entry should be something
you'd be comfortable defending in an audit: "ML is just short for
Machine Learning" is defensible; "Python implies Django" is not.
"""

from __future__ import annotations

import re

# alias (lowercase) -> canonical form (lowercase)
SKILL_ALIASES: dict[str, str] = {
    "ml": "machine learning",
    "dl": "deep learning",
    "cv": "computer vision",
    "nlp": "natural language processing",
    "ai": "artificial intelligence",
    "genai": "generative ai",
    "gen ai": "generative ai",
    "llm": "large language model",
    "llms": "large language model",
    "cnn": "convolutional neural network",
    "rnn": "recurrent neural network",
    "js": "javascript",
    "ts": "typescript",
    "k8s": "kubernetes",
    "rl": "reinforcement learning",
}


def _clean(text: str) -> str:
    """Lowercase and collapse whitespace, but change nothing else."""
    return re.sub(r"\s+", " ", text.strip().lower())


def normalize_skill(skill: str) -> str:
    """
    Return the canonical lowercase form of a skill name: its alias
    target if one is defined, otherwise the cleaned original. This is
    the ONLY place alias resolution happens — everything else should
    call this or skills_equal() rather than comparing strings itself.
    """
    cleaned = _clean(skill)
    return SKILL_ALIASES.get(cleaned, cleaned)


def skills_equal(a: str, b: str) -> bool:
    """Whether two skill names refer to the same skill after normalization."""
    return normalize_skill(a) == normalize_skill(b)


def find_match(skill: str, candidate_skills: list[str]) -> str | None:
    """
    Return the first entry in candidate_skills that matches `skill`
    after normalization, or None if there's no match. Returning the
    matched string (not just a bool) lets callers report exactly what
    was matched against, for the explanation output.
    """
    for candidate_skill in candidate_skills:
        if skills_equal(skill, candidate_skill):
            return candidate_skill
    return None
