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
    ) -> None:
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}  # selector -> FakeElement or list[FakeElement]

    def inner_text(self) -> str:
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


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self._elements: dict[str, Any] = {}  # selector -> FakeElement or list[FakeElement]
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.goto_calls: list[str] = []
        self.content_html = "<html></html>"
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

    def goto(self, url: str) -> None:
        self.goto_calls.append(url)
        self.url = url
        if self.on_goto is not None:
            self.on_goto(url)

    def wait_for_load_state(self, state: str = "load") -> None:
        if self.wait_for_load_state_error is not None:
            raise self.wait_for_load_state_error

    def fill(self, selector: str, value: str) -> None:
        if self.fill_error is not None:
            raise self.fill_error
        self.filled[selector] = value

    def click(self, selector: str) -> None:
        self.clicked.append(selector)
        if self.on_click is not None:
            self.on_click(selector)

    def query_selector(self, selector: str) -> "FakeElement | None":
        result = self._elements.get(selector)
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def query_selector_all(self, selector: str) -> list["FakeElement"]:
        result = self._elements.get(selector)
        if result is None:
            return []
        return result if isinstance(result, list) else [result]

    def content(self) -> str:
        return self.content_html

    def screenshot(self, path: str, full_page: bool = True) -> None:
        Path(path).write_bytes(b"fake-png-bytes")

    # --- test helper, not part of the real Playwright API ---
    def set_element(self, selector: str, element: Any) -> None:
        self._elements[selector] = element
