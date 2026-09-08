from naukri_agent.matching.skill_normalizer import find_match, normalize_skill, skills_equal


def test_known_alias_normalizes_to_canonical():
    assert normalize_skill("ML") == "machine learning"
    assert normalize_skill("CV") == "computer vision"


def test_alias_and_canonical_are_equal():
    assert skills_equal("ML", "Machine Learning") is True
    assert skills_equal("cv", "Computer Vision") is True


def test_case_and_whitespace_insensitive():
    assert skills_equal("  Python  ", "python") is True


def test_unrelated_skills_are_not_equal():
    assert skills_equal("Python", "Django") is False
    assert skills_equal("Python", "FastAPI") is False


def test_no_dangerous_capability_inference():
    """
    The exact case called out in the requirements: knowing Python
    does not imply Django or FastAPI, and the alias table must never
    encode that kind of relationship.
    """
    assert normalize_skill("Python") != normalize_skill("Django")
    assert normalize_skill("Python") != normalize_skill("FastAPI")


def test_find_match_returns_the_matched_candidate_skill():
    result = find_match("ML", ["SQL", "Machine Learning", "Python"])
    assert result == "Machine Learning"


def test_find_match_returns_none_when_absent():
    assert find_match("AWS", ["Python", "SQL"]) is None
