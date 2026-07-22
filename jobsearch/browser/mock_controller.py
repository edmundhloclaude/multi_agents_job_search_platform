"""Mock BrowserController for tests (spec §1, §7).

Scripted pages + recorded actions so tests never touch a real browser.
Can simulate login walls / CAPTCHAs to exercise the human-handoff path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .controller import BrowserController, PageView


@dataclass
class MockPage:
    url: str
    text: str = ""
    dom: str = ""
    links: list[str] = field(default_factory=list)
    # selector -> list of element text values (for query()).
    elements: dict[str, list[str]] = field(default_factory=dict)
    requires_login: bool = False
    requires_captcha: bool = False


class MockBrowserController(BrowserController):
    """In-memory browser. ``submit_enabled`` is configurable for gate tests."""

    def __init__(self, pages: Optional[dict[str, MockPage]] = None,
                 *, submit_enabled: bool = False):
        self.pages: dict[str, MockPage] = pages or {}
        self.submit_enabled = submit_enabled
        self._current: Optional[MockPage] = None
        # Recorded mutations, so tests can assert what did / did not happen.
        self.actions: list[tuple[str, tuple]] = []

    # -- test setup helper --------------------------------------------- #
    def add_page(self, page: MockPage) -> None:
        self.pages[page.url] = page

    # -- read operations ----------------------------------------------- #
    def open(self, url: str) -> PageView:
        page = self.pages.get(url)
        if page is None:
            page = MockPage(url=url, text="", dom="")
        self._current = page
        return self._view(page)

    def _view(self, page: MockPage) -> PageView:
        return PageView(
            url=page.url, text=page.text, dom=page.dom, links=list(page.links),
            requires_login=page.requires_login, requires_captcha=page.requires_captcha,
        )

    def current_url(self) -> str:
        return self._current.url if self._current else ""

    def page_text(self) -> str:
        return self._current.text if self._current else ""

    def page_dom(self) -> str:
        return self._current.dom if self._current else ""

    def query(self, selector: str) -> list[str]:
        if not self._current:
            return []
        return list(self._current.elements.get(selector, []))

    def needs_human(self) -> bool:
        if not self._current:
            return False
        return self._current.requires_login or self._current.requires_captcha

    # -- mutating operations (recorded; require submit_enabled) --------- #
    def type_text(self, selector: str, text: str) -> None:
        if not self.submit_enabled:
            raise PermissionError("submit_enabled is False; type_text not permitted.")
        self.actions.append(("type_text", (selector, text)))

    def click(self, selector: str) -> None:
        if not self.submit_enabled:
            raise PermissionError("submit_enabled is False; click not permitted.")
        self.actions.append(("click", (selector,)))

    def submit(self) -> None:
        if not self.submit_enabled:
            raise PermissionError("submit_enabled is False; submit not permitted.")
        self.actions.append(("submit", ()))


class MockComputer:
    """A fake Computer backend for OpenAIComputerUseController tests.

    Serves a fixed PNG screenshot and records performed actions, so the
    controller's read-only enforcement can be tested without a real browser.
    Implements the ``Computer`` interface structurally (duck-typed).
    """

    def __init__(self, png_bytes: bytes = b"", *, url: str = "https://example.test",
                 dimensions: tuple[int, int] = (1024, 768)):
        # A minimal valid 1x1 PNG if none supplied.
        self._png = png_bytes or (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
            b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        self._url = url
        self._dims = dimensions
        self.actions: list[dict] = []

    def goto(self, url: str) -> None:
        self._url = url

    def current_url(self) -> str:
        return self._url

    def screenshot(self) -> bytes:
        return self._png

    def dimensions(self) -> tuple[int, int]:
        return self._dims

    def perform(self, action: dict) -> None:
        self.actions.append(action)
