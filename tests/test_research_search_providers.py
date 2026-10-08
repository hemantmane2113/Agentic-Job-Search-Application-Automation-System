"""Search providers for the researcher: Serper (free starter allowance) and Brave (paid), and how one is chosen."""

from __future__ import annotations

import httpx
import pytest

from naukri_agent.config import Settings
from naukri_agent.research_agent.runner import build_search
from naukri_agent.research_agent.web import BraveSearch, SearchError, SerperSearch


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def serper(payload=None, status=200, text=None, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(req)
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=payload or {})

    return SerperSearch("KEY123", client=client_for(handler))


def test_serper_parses_results_sends_the_key_in_a_header_and_clamps_the_count():
    import json

    seen = []
    payload = {"organic": [
        {"title": "<b>Acme</b> Careers", "link": "https://acme.example/careers", "snippet": "Join <i>us</i>"},
        {"title": "Insecure", "link": "http://insecure.example/", "snippet": "x"},
        {"title": "No link", "snippet": "y"},
    ]}
    hits = serper(payload, seen=seen).search("Acme careers", count=99)
    assert [(h.title, h.url, h.snippet) for h in hits] == [("Acme Careers", "https://acme.example/careers", "Join us")]
    req = seen[0]
    assert req.method == "POST" and req.headers["x-api-key"] == "KEY123"
    body = json.loads(req.content)
    assert body["q"] == "Acme careers" and body["num"] == 8 and "KEY123" not in req.url.query.decode()


def test_serper_cleans_the_query_and_rejects_an_empty_one():
    import json

    seen = []
    serper({}, seen=seen).search("Acme\n\tcareers\x00 page")
    assert json.loads(seen[0].content)["q"] == "Acme  careers  page"
    with pytest.raises(SearchError, match="empty"):
        serper({}).search(" \n ")


@pytest.mark.parametrize(
    "status, text, expected",
    [
        (429, None, "rate limit"),
        (401, None, "rejected"),
        (403, None, "rejected"),
        (400, '{"message":"Not enough credits"}', "credits are used up"),
        (400, '{"message":"bad request"}', "HTTP 400"),
        (500, None, "HTTP 500"),
    ],
)
def test_serper_failures_are_plain_errors_that_never_contain_the_key(status, text, expected):
    with pytest.raises(SearchError, match=expected) as exc:
        serper({}, status=status, text=text).search("x")
    assert "KEY123" not in str(exc.value)


def test_serper_network_and_garbage_responses_are_search_errors():
    def boom(req):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(SearchError, match="network error"):
        SerperSearch("k", client=client_for(boom)).search("x")
    with pytest.raises(SearchError, match="unreadable"):
        serper(status=200, text="<html>not json</html>").search("x")


# --- choosing a provider -----------------------------------------------------------------------------------------------


def cfg(**kw):
    return Settings(_env_file=None, **kw)


def test_with_no_key_there_is_no_search():
    assert build_search(cfg()) is None


def test_auto_prefers_serper_then_brave():
    assert isinstance(build_search(cfg(serper_api_key="s")), SerperSearch)
    assert isinstance(build_search(cfg(brave_api_key="b")), BraveSearch)
    assert isinstance(build_search(cfg(serper_api_key="s", brave_api_key="b")), SerperSearch)


def test_a_named_provider_is_used_only_if_its_own_key_exists():
    both = dict(serper_api_key="s", brave_api_key="b")
    assert isinstance(build_search(cfg(research_search_provider="brave", **both)), BraveSearch)
    assert isinstance(build_search(cfg(research_search_provider="serper", **both)), SerperSearch)
    assert build_search(cfg(research_search_provider="serper", brave_api_key="b")) is None  # no silent switch to a paid provider
    assert build_search(cfg(research_search_provider="brave", serper_api_key="s")) is None


def test_none_turns_search_off_even_when_keys_exist():
    assert build_search(cfg(research_search_provider="none", serper_api_key="s", brave_api_key="b")) is None


def test_provider_choice_is_case_insensitive():
    assert isinstance(build_search(cfg(research_search_provider="SERPER", serper_api_key="s")), SerperSearch)
