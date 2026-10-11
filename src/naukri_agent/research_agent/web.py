"""
Web access for the company-site job researcher: a guarded page fetch and a search client.

Everything an agent reads from the web is UNTRUSTED text. This module's job is to make sure the
agent can only do two harmless things: read a public https page, and run a web search.

  * Only https, only the default port, no credentials in the URL.
  * The host must resolve ONLY to public addresses: loopback, private networks, link-local
    (cloud metadata), multicast and reserved ranges are refused, for the first request and for
    every redirect hop (redirects are followed by hand, at most 3).
  * Only text pages (html / plain text), cut off at a byte limit, short timeout.
  * No cookies, no login, no JavaScript: a plain GET and a text extraction.

Known limit, stated plainly: the address is checked just before the request, not pinned through it,
so a hostile DNS server that changes its answer between the check and the request could slip
through. For a read-only tool on a personal PC fetching a handful of career pages this is accepted.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

USER_AGENT = "naukri-agent-research/1.0 (personal job-search helper; read-only)"
_ALLOWED_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
_MAX_REDIRECTS = 3
_TEXT_CHARS = 6000
_MAX_LINKS = 15
_CAREER_WORDS = re.compile(r"career|job|join|opening|vacanc|hiring|apply|work-with|talent|recruit|position", re.I)


class UnsafeUrlError(ValueError):
    """The URL is not one the agent may open."""


class FetchError(RuntimeError):
    """The page could not be fetched (network, status, type)."""


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local)


def normalize_url(url: str) -> str:
    """Comparable form of a URL: lower-case host, no fragment, no tracking params, no trailing slash."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")])
    path = parts.path.rstrip("/") or ""
    return urlunsplit((parts.scheme.lower(), (parts.hostname or "").lower() + (f":{parts.port}" if parts.port else ""), path, query, ""))


def check_public_https_url(url: str, resolver: Callable[..., Any] = socket.getaddrinfo) -> str:
    """Return the URL (without fragment) if it is safe to open, else raise UnsafeUrlError."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("malformed URL") from exc
    if parts.scheme != "https":
        raise UnsafeUrlError("only https URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeUrlError("credentials in a URL are not allowed")
    if port not in (None, 443):
        raise UnsafeUrlError("only the default https port is allowed")
    host = parts.hostname
    if not host:
        raise UnsafeUrlError("no host")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            infos = resolver(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeUrlError("host cannot be resolved") from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError as exc:
                raise UnsafeUrlError("host resolved to an unreadable address") from exc
        if not addresses:
            raise UnsafeUrlError("host cannot be resolved")
    if not all(_is_public(a) for a in addresses):
        raise UnsafeUrlError("host is not on the public internet")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


# --- HTML -> text ------------------------------------------------------------------------------------------------------


class _Extractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}
    _BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._chunks: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []
        if tag in self._BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            self.links.append((" ".join("".join(self._link_text).split())[:80], self._href))
            self._href = None
        if tag in self._BREAK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._skip_depth:
            return
        self._chunks.append(data)
        if self._href is not None:
            self._link_text.append(data)


@dataclass
class FetchedPage:
    url: str  # final URL after redirects
    title: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (text, absolute https URL): career-looking only
    truncated: bool = False


def html_to_page(html: str, base_url: str) -> tuple[str, str, list[tuple[str, str]]]:
    parser = _Extractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - broken HTML: use whatever was extracted
        pass
    if parser._href is not None:  # a link left open when the page ended
        parser.links.append((" ".join("".join(parser._link_text).split())[:80], parser._href))
    lines = [" ".join(line.split()) for line in "".join(parser._chunks).splitlines()]
    text = "\n".join(line for line in lines if line)[:_TEXT_CHARS]
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for label, href in parser.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        if not absolute.startswith("https://") or absolute in seen:
            continue
        if _CAREER_WORDS.search(label) or _CAREER_WORDS.search(absolute):
            seen.add(absolute)
            links.append((label, absolute))
        if len(links) >= _MAX_LINKS:
            break
    return " ".join(parser.title.split())[:150], text, links


def _download(
    url: str,
    *,
    client: httpx.Client | None,
    max_bytes: int,
    timeout: float,
    resolver: Callable[..., Any],
) -> tuple[str, str, str, bool]:
    """GET one public https page, with every guard. Returns (final_url, text, content_type, truncated)."""
    own = client is None
    http = client or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"})
    try:
        current = check_public_https_url(url, resolver)
        for _hop in range(_MAX_REDIRECTS + 1):
            try:
                with http.stream("GET", current, follow_redirects=False) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError("redirect without a location")
                        current = check_public_https_url(urljoin(current, location), resolver)
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in _ALLOWED_TYPES:
                        raise FetchError(f"unsupported content type: {ctype or 'unknown'}")
                    body = bytearray()
                    truncated = False
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) >= max_bytes:
                            truncated = True
                            del body[max_bytes:]
                            break
                    text = bytes(body).decode(resp.encoding or "utf-8", errors="replace")
            except httpx.HTTPError as exc:
                raise FetchError(f"network error ({type(exc).__name__})") from exc
            return current, text, ctype, truncated
        raise FetchError("too many redirects")
    finally:
        if own:
            http.close()


def fetch_page(
    url: str,
    *,
    client: httpx.Client | None = None,
    max_bytes: int = 300_000,
    timeout: float = 15.0,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> FetchedPage:
    """GET one public https page and return its text. Raises UnsafeUrlError or FetchError."""
    current, html, ctype, truncated = _download(url, client=client, max_bytes=max_bytes, timeout=timeout, resolver=resolver)
    if ctype == "text/plain":
        return FetchedPage(url=current, title="", text=" ".join(html.split())[:_TEXT_CHARS], truncated=truncated)
    title, text, links = html_to_page(html, current)
    return FetchedPage(url=current, title=title, text=text, links=links, truncated=truncated)


def fetch_raw(
    url: str,
    *,
    client: httpx.Client | None = None,
    max_bytes: int = 400_000,
    timeout: float = 15.0,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> tuple[str, str]:
    """Same guards as fetch_page, but returns (final_url, the page's raw HTML). Used to recognise which
    application system a careers page uses; the HTML is searched by code and never shown to the model."""
    current, html, _ctype, _truncated = _download(url, client=client, max_bytes=max_bytes, timeout=timeout, resolver=resolver)
    return current, html


# --- web search ------------------------------------------------------------------------------------------------------------


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


class SearchError(RuntimeError):
    pass


_TAGS = re.compile(r"<[^>]+>")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class BraveSearch:
    """Brave Search API (https://api.search.brave.com). The query carries a company name, never personal data."""

    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, client: httpx.Client | None = None, timeout: float = 15.0) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    def search(self, query: str, count: int = 5) -> list[SearchHit]:
        q = _CONTROL.sub(" ", query).strip()[:200]
        if not q:
            raise SearchError("empty query")
        try:
            resp = self._client.get(
                self.URL,
                params={"q": q, "count": max(1, min(count, 8))},
                headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise SearchError(f"network error ({type(exc).__name__})") from exc
        if resp.status_code == 429:
            raise SearchError("search rate limit reached")
        if resp.status_code in (401, 403):
            raise SearchError("search key was rejected")
        if resp.status_code >= 400:
            raise SearchError(f"search failed (HTTP {resp.status_code})")
        try:
            results = resp.json().get("web", {}).get("results", [])
        except ValueError as exc:
            raise SearchError("unreadable search response") from exc
        hits = []
        for r in results:
            url = str(r.get("url", ""))
            if not url.startswith("https://"):
                continue
            hits.append(
                SearchHit(
                    title=_TAGS.sub("", str(r.get("title", "")))[:150],
                    url=url,
                    snippet=_TAGS.sub("", str(r.get("description", "")))[:300],
                )
            )
        return hits


class SerperSearch:
    """Serper (https://serper.dev), Google results as JSON. Same interface as BraveSearch."""

    URL = "https://google.serper.dev/search"

    def __init__(self, api_key: str, client: httpx.Client | None = None, timeout: float = 15.0) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    def search(self, query: str, count: int = 5) -> list[SearchHit]:
        q = _CONTROL.sub(" ", query).strip()[:200]
        if not q:
            raise SearchError("empty query")
        try:
            resp = self._client.post(
                self.URL,
                json={"q": q, "num": max(1, min(count, 8)), "gl": "in"},
                headers={"X-API-KEY": self._key, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise SearchError(f"network error ({type(exc).__name__})") from exc
        if resp.status_code == 429:
            raise SearchError("search rate limit reached")
        if resp.status_code in (401, 403):
            raise SearchError("search key was rejected")
        if resp.status_code == 400 and "credit" in resp.text.lower():
            raise SearchError("search credits are used up")
        if resp.status_code >= 400:
            raise SearchError(f"search failed (HTTP {resp.status_code})")
        try:
            results = resp.json().get("organic", [])
        except ValueError as exc:
            raise SearchError("unreadable search response") from exc
        hits = []
        for r in results:
            url = str(r.get("link", ""))
            if not url.startswith("https://"):
                continue
            hits.append(
                SearchHit(
                    title=_TAGS.sub("", str(r.get("title", "")))[:150],
                    url=url,
                    snippet=_TAGS.sub("", str(r.get("snippet", "")))[:300],
                )
            )
        return hits
