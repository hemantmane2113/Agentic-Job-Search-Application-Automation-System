"""
Fake Playwright-like Page/ElementHandle objects. Implements exactly
the subset of the real API that browser/*.py calls, so orchestration
logic (login flow classification, listing parsing, ...) can be tested
without a real browser, real network, or real Naukri credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class FakeElement:
    def __init__(
        self,
        text: str = "",
        attrs: dict[str, str] | None = None,
        children: dict[str, Any] | None = None,
        tag: str | None = None,
        outer_html: str | None = None,
    ) -> None:
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}  # selector -> FakeElement or list[FakeElement]
        self._tag = tag
        self._outer_html = outer_html

    def inner_text(self) -> str:
        return self._text

    def text_content(self) -> str:
        # Playwright's ElementHandle.text_content — unlike inner_text this
        # works on non-rendered nodes such as <script> tags.
        return self._text

    def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    def query_selector(self, selector: str) -> "FakeElement | None":
        result = self._children.get(selector)
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def query_selector_all(self, selector: str) -> list["FakeElement"]:
        result = self._children.get(selector)
        if result is None:
            return []
        return result if isinstance(result, list) else [result]

    def evaluate(self, script: str, arg: Any = None) -> Any:
        """Minimal stand-in for ElementHandle.evaluate — only the tiny
        set of `el => ...` expressions browser/apply_inspection.py uses."""
        if "outerHTML" in script:
            if self._outer_html is not None:
                return self._outer_html
            t = self._tag or "div"
            return f"<{t}>{self._text}</{t}>"
        if "tagName" in script:
            return self._tag
        if "disabled" in script:
            return self._attrs.get("disabled") is not None
        if "textContent" in script:
            return self._text
        return None


class FakeRequest:
    """Stand-in for playwright.sync_api.Request — only the fields
    browser/apply_inspection.py's route handler reads. `headers` is a
    property that COUNTS accesses so a test can prove the guard never
    reads request headers (cookies / auth)."""

    def __init__(
        self,
        method: str,
        url: str,
        resource_type: str = "xhr",
        post_data: str | None = None,
        is_navigation: bool = False,
    ) -> None:
        self.method = method
        self.url = url
        self.resource_type = resource_type
        self.post_data = post_data
        self._is_navigation = is_navigation
        self.headers_access_count = 0

    def is_navigation_request(self) -> bool:
        return self._is_navigation

    @property
    def headers(self) -> dict[str, str]:
        self.headers_access_count += 1
        return {}


class FakeAPIResponse:
    """Stand-in for playwright.sync_api.APIResponse (what route.fetch()
    returns). Only status / headers / body are read by the guard."""

    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        body_error: Exception | None = None,
    ) -> None:
        self.status = status
        self._headers = headers or {}
        self._body = body
        self._body_error = body_error
        self.body_calls = 0

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    def body(self) -> bytes:
        self.body_calls += 1
        if self._body_error is not None:
            raise self._body_error
        return self._body


class FakeResponse:
    """Stand-in for playwright.sync_api.Response (delivered to a
    `page.on('response', ...)` listener). `status` and `headers` come
    from the response head; `body()` can be made to RAISE to simulate a
    navigation / context-close race."""

    def __init__(
        self,
        method: str,
        url: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes | None = b"",
        body_error: Exception | None = None,
        status_error: Exception | None = None,
    ) -> None:
        self.request = FakeRequest(method, url)
        self._status = status
        self._status_error = status_error
        self._headers = headers or {}
        self._body = body
        self._body_error = body_error
        self.body_calls = 0

    @property
    def status(self) -> int:
        if self._status_error is not None:
            raise self._status_error
        return self._status

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    def body(self) -> bytes:
        self.body_calls += 1
        if self._body_error is not None:
            raise self._body_error
        if self._body is None:
            raise RuntimeError("FakeResponse: body not configured")
        return self._body


class FakeFrame:
    """Stand-in for playwright.sync_api.Frame (delivered to
    `framenavigated` / `frameattached` / `framedetached` listeners).
    `url` can be made to RAISE to simulate a navigation / close race."""

    def __init__(
        self, url: str, url_error: Exception | None = None, parent_frame: Any = None
    ) -> None:
        self._url = url
        self._url_error = url_error
        self.parent_frame = parent_frame

    @property
    def url(self) -> str:
        if self._url_error is not None:
            raise self._url_error
        return self._url


class FakeConsoleMessage:
    """Stand-in for playwright.sync_api.ConsoleMessage."""

    def __init__(
        self, level: str, text: str, url: str | None = None, line: int | None = None
    ) -> None:
        self.type = level
        self.text = text
        self.location = {"url": url, "lineNumber": line} if (url or line) else {}


class FakePageError(Exception):
    """Stand-in for the Error passed to a `pageerror` listener."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _FakeWSFrame:
    def __init__(self, payload: Any) -> None:
        self.payload = payload


class FakeWebSocket:
    """Stand-in for playwright.sync_api.WebSocket. Only `url` and the
    `framesent` / `framereceived` / `close` events are used."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._listeners: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def simulate_frame(self, direction: str, payload: Any) -> None:
        event = "framesent" if direction == "sent" else "framereceived"
        for handler in self._listeners.get(event, []):
            handler(_FakeWSFrame(payload))

    def simulate_close(self) -> None:
        for handler in self._listeners.get("close", []):
            handler(self)


class FakeRoute:
    """Stand-in for playwright.sync_api.Route. Records which terminal
    action the handler took."""

    def __init__(self, request: FakeRequest, fetch_response: Any = None,
                 fetch_error: Exception | None = None) -> None:
        self.request = request
        self.action: str | None = None
        self.abort_error_code: str | None = None
        self.fetch_calls = 0
        self.fulfilled_with_response = False
        self._fetch_response = fetch_response
        self._fetch_error = fetch_error

    def continue_(self, **kwargs: Any) -> None:
        self.action = "continue"

    def abort(self, error_code: str = "failed") -> None:
        self.action = "abort"
        self.abort_error_code = error_code

    def fulfill(self, **kwargs: Any) -> None:
        self.action = "fulfill"
        self.fulfilled_with_response = "response" in kwargs

    def fetch(self, **kwargs: Any) -> Any:
        self.fetch_calls += 1
        if self._fetch_error is not None:
            raise self._fetch_error
        if self._fetch_response is None:
            raise RuntimeError("FakeRoute.fetch(): no fake response configured")
        return self._fetch_response


class FakePage:
    def __init__(self) -> None:
        # `url` is a property (see below) so a test can make it RAISE,
        # the way Playwright's real `url` does once the driver
        # connection is dead (browser window closed mid-CAPTCHA).
        self._connection_error: Exception | None = None
        self._url = "about:blank"
        self._elements: dict[str, Any] = {}  # selector -> FakeElement or list[FakeElement]
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.goto_calls: list[str] = []
        self.content_html = "<html></html>"
        self.page_title = ""
        # Request-routing seam (browser/apply_inspection.py).
        self.routes: list[tuple[str, Any]] = []
        self.unroute_calls: list[tuple[str, Any]] = []
        self.requests: list[FakeRequest] = []
        # Optional hooks a test can set to mutate page state in
        # response to navigation/clicks — e.g. simulating an
        # authenticated-session redirect during goto(), or a
        # CAPTCHA/MFA element appearing after a click().
        self.on_goto: Callable[[str], None] | None = None
        self.on_click: Callable[[str], None] | None = None
        # If set, fill()/wait_for_load_state() raise this instead of
        # acting normally — used to simulate a Playwright-side error
        # (e.g. a real TimeoutError) during an interaction.
        self.fill_error: Exception | None = None
        self.wait_for_load_state_error: Exception | None = None
        self.load_state_calls: list[tuple[str, float | None]] = []
        self.default_timeout_ms: float | None = None
        # Event listeners: {"response": [handler, ...], ...}
        self.event_listeners: dict[str, list[Any]] = {}
        # wait_for_selector seam: on a miss, on_wait_for_selector(sel) is
        # called (a test uses it to "land" a redirect / reveal a
        # challenge); still nothing -> wait_for_selector_error or a
        # TimeoutError is raised, mirroring a real bounded timeout.
        self.wait_for_selector_calls: list[tuple[str, str | None, float | None]] = []
        self.on_wait_for_selector: Callable[[str], None] | None = None
        self.wait_for_selector_error: Exception | None = None
        # Client-workflow observation seams.
        self.init_scripts: list[str] = []
        self.on_evaluate: Callable[[str], Any] | None = None
        self.wait_for_timeout_calls: list[float] = []

    # --- url as a property so it can be made to RAISE (dead driver) ---

    @property
    def url(self) -> str:
        if self._connection_error is not None:
            raise self._connection_error
        return self._url

    @url.setter
    def url(self, value: str) -> None:
        self._url = value

    def simulate_connection_loss(
        self, error: Exception | None = None
    ) -> None:
        """After this, every page interaction (and reading `url`) raises,
        the way a real Playwright page does once its driver connection
        is gone — e.g. the user closed the browser window during a
        manual CAPTCHA."""
        self._connection_error = error or RuntimeError(
            "Connection closed while reading from the driver"
        )

    def _check_conn(self) -> None:
        if self._connection_error is not None:
            raise self._connection_error

    def goto(self, url: str) -> None:
        self._check_conn()
        self.goto_calls.append(url)
        self.url = url
        if self.on_goto is not None:
            self.on_goto(url)

    def wait_for_load_state(self, state: str = "load", timeout: float | None = None) -> None:
        self.load_state_calls.append((state, timeout))
        self._check_conn()
        if self.wait_for_load_state_error is not None:
            raise self.wait_for_load_state_error

    def set_default_timeout(self, timeout_ms: float) -> None:
        self.default_timeout_ms = timeout_ms

    def wait_for_selector(
        self, selector: str, state: str | None = None, timeout: float | None = None
    ) -> "FakeElement | None":
        self.wait_for_selector_calls.append((selector, state, timeout))
        parts = [p.strip() for p in selector.split(",")] if selector else []

        def _hit() -> "FakeElement | None":
            for p in (selector, *parts):
                el = self._elements.get(p)
                if el is not None:
                    return el[0] if isinstance(el, list) else el
            return None

        found = _hit()
        if found is not None:
            return found
        if self.on_wait_for_selector is not None:
            self.on_wait_for_selector(selector)
            found = _hit()
            if found is not None:
                return found
        if self.wait_for_selector_error is not None:
            raise self.wait_for_selector_error
        raise TimeoutError(f"wait_for_selector({selector!r}) timed out")

    def fill(self, selector: str, value: str) -> None:
        self._check_conn()
        if self.fill_error is not None:
            raise self.fill_error
        self.filled[selector] = value

    def click(self, selector: str) -> None:
        self._check_conn()
        self.clicked.append(selector)
        if self.on_click is not None:
            self.on_click(selector)

    def query_selector(self, selector: str) -> "FakeElement | None":
        self._check_conn()
        result = self._elements.get(selector)
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def query_selector_all(self, selector: str) -> list["FakeElement"]:
        self._check_conn()
        result = self._elements.get(selector)
        if result is None:
            return []
        return result if isinstance(result, list) else [result]

    def content(self) -> str:
        self._check_conn()
        return self.content_html

    def title(self) -> str:
        self._check_conn()
        return self.page_title

    def screenshot(self, path: str, full_page: bool = True) -> None:
        self._check_conn()
        Path(path).write_bytes(b"fake-png-bytes")

    def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    def unroute(self, pattern: str, handler: Any = None) -> None:
        self.unroute_calls.append((pattern, handler))

    def on(self, event: str, handler: Any) -> None:
        self.event_listeners.setdefault(event, []).append(handler)

    # --- test helpers, not part of the real Playwright API ---
    def set_element(self, selector: str, element: Any) -> None:
        self._elements[selector] = element

    def simulate_request(
        self,
        method: str,
        url: str,
        resource_type: str = "xhr",
        post_data: str | None = None,
        is_navigation: bool = False,
        fetch_response: Any = None,
        fetch_error: Exception | None = None,
    ) -> FakeRoute:
        """Feed one request through every installed route handler and
        return the FakeRoute so the test can assert continue vs abort.
        `fetch_response` / `fetch_error` back route.fetch() for the
        apply-init allowlist path."""
        if not self.routes:
            raise AssertionError("simulate_request() called but no route handler is installed")
        request = FakeRequest(
            method, url, resource_type=resource_type, post_data=post_data, is_navigation=is_navigation
        )
        self.requests.append(request)
        route = FakeRoute(request, fetch_response=fetch_response, fetch_error=fetch_error)
        for _pattern, handler in self.routes:
            handler(route)
        return route

    def simulate_response(
        self,
        method: str,
        url: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes | None = b"",
        body_error: Exception | None = None,
        status_error: Exception | None = None,
        resource_type: str = "xhr",
    ) -> FakeResponse:
        """Fire one `response` event through every `page.on('response')`
        listener (how MutatingRequestBlocker observes responses now)."""
        resp = FakeResponse(
            method, url, status=status, headers=headers, body=body,
            body_error=body_error, status_error=status_error,
        )
        resp.request.resource_type = resource_type
        for handler in self.event_listeners.get("response", []):
            handler(resp)
        return resp

    def simulate_navigation(self, url: str, url_error: Exception | None = None) -> FakeFrame:
        """Fire one `framenavigated` event through every
        `page.on('framenavigated')` listener."""
        frame = FakeFrame(url, url_error=url_error)
        for handler in self.event_listeners.get("framenavigated", []):
            handler(frame)
        return frame

    # --- client-workflow observation seams ---

    def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        if self.on_evaluate is not None:
            return self.on_evaluate(expression)
        return None

    def wait_for_timeout(self, timeout_ms: float) -> None:
        self.wait_for_timeout_calls.append(timeout_ms)

    def simulate_console(
        self, text: str, level: str = "error", url: str | None = None, line: int | None = None
    ) -> None:
        msg = FakeConsoleMessage(level, text, url=url, line=line)
        for handler in self.event_listeners.get("console", []):
            handler(msg)

    def simulate_pageerror(self, message: str) -> None:
        err = FakePageError(message)
        for handler in self.event_listeners.get("pageerror", []):
            handler(err)

    def simulate_websocket(self, url: str) -> FakeWebSocket:
        ws = FakeWebSocket(url)
        for handler in self.event_listeners.get("websocket", []):
            handler(ws)
        return ws

    def simulate_frame_attached(self, url: str, parent_frame: Any = None) -> FakeFrame:
        frame = FakeFrame(url, parent_frame=parent_frame)
        for handler in self.event_listeners.get("frameattached", []):
            handler(frame)
        return frame

    def simulate_frame_detached(self, url: str) -> FakeFrame:
        frame = FakeFrame(url)
        for handler in self.event_listeners.get("framedetached", []):
            handler(frame)
        return frame
