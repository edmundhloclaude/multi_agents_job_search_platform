"""DOM-native BrowserController backed by real Chromium via Playwright.

Reliable path for reading and filling real application forms without needing a
vision or computer-use model. Reads happen through the DOM; the special selector
``form_field`` returns JSON field descriptors (name/label/type/required/selector)
that the Applier's map phase consumes directly.

Trust tiers are preserved: mutating methods (type_text/click/submit) raise unless
``submit_enabled`` is True, so a READ_BROWSER unit wrapped in ReadOnlyBrowser can
never fill or submit. The orchestrator only builds a submit-capable instance
inside the human-gated submit flow.
"""

from __future__ import annotations

import json
from typing import Optional

from .controller import BrowserController, PageView
from .playwright_computer import PlaywrightComputer

#: Selector name the Applier's map phase queries for form fields.
FORM_FIELD_SELECTOR = "form_field"


class PlaywrightBrowserController(BrowserController):
    def __init__(self, *, submit_enabled: bool = False, headless: bool = True,
                 computer: Optional[PlaywrightComputer] = None):
        self.submit_enabled = submit_enabled
        self._computer = computer or PlaywrightComputer(headless=headless)

    # -- read operations ----------------------------------------------- #
    def open(self, url: str) -> PageView:
        self._computer.goto(url)
        return PageView(
            url=self._computer.current_url(),
            text=self._computer.page_text(),
            requires_login=self._computer.has_human_gate(),
        )

    def current_url(self) -> str:
        return self._computer.current_url()

    def page_text(self) -> str:
        return self._computer.page_text()

    def page_dom(self) -> str:
        return self._computer.page_html()

    def query(self, selector: str) -> list[str]:
        if selector == FORM_FIELD_SELECTOR:
            # Structured form fields as JSON strings (Applier json.loads each).
            return [json.dumps(fd) for fd in self._computer.form_fields()]
        # Generic: return inner text of matching elements.
        try:
            page = self._computer._page
            return [el.inner_text() for el in page.query_selector_all(selector)]
        except Exception:
            return []

    def needs_human(self) -> bool:
        return self._computer.has_human_gate()

    # -- mutating operations (gated) ----------------------------------- #
    def type_text(self, selector: str, text: str) -> None:
        if not self.submit_enabled:
            raise PermissionError("read-only controller: type_text not permitted.")
        self._computer.perform({"type": "type", "selector": selector, "text": text})

    def click(self, selector: str) -> None:
        if not self.submit_enabled:
            raise PermissionError("read-only controller: click not permitted.")
        self._computer.perform({"type": "click", "selector": selector})

    def submit(self) -> None:
        if not self.submit_enabled:
            raise PermissionError("read-only controller: submit not permitted.")
        self._computer.perform({"type": "submit"})

    def close(self) -> None:
        self._computer.close()
