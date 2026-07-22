"""Claude-in-Chrome (CUA / computer-use) BrowserController implementation.

This is the real-browser backend for READ_BROWSER (Crawler) and GATED
(Applier submit) tiers. It is intentionally a thin, documented stub: wiring it
to a live Chrome instance requires the Claude computer-use runtime and a real
browser, which are out of scope for the offline test suite. Every method
raises ``NotImplementedError`` with guidance so the failure is explicit rather
than silent.

Design notes for whoever wires this up:

* Implement ``open``/``page_text``/``page_dom``/``query`` via CUA screen reads
  and DOM extraction. Keep them side-effect free.
* ``needs_human`` should detect login forms / CAPTCHA challenges and return
  True so the caller pauses and hands off (spec §0.5) — never type credentials
  or solve CAPTCHAs here.
* ``type_text``/``click``/``submit`` must remain guarded by ``submit_enabled``.
  The orchestrator only ever constructs a submit-capable CUA controller behind
  the human approval gate (spec §5); READ_BROWSER units get a ReadOnlyBrowser
  wrapper instead.
"""

from __future__ import annotations

from .controller import BrowserController, PageView

_MSG = (
    "CUABrowserController is a stub. Wire it to the Claude-in-Chrome computer-use "
    "runtime to drive a real Chrome instance. Use MockBrowserController for tests."
)


class CUABrowserController(BrowserController):
    def __init__(self, *, submit_enabled: bool = False, model: str = "claude-opus-4-8"):
        # submit_enabled is set True only when the orchestrator builds the
        # GATED submit controller behind the approval gate.
        self.submit_enabled = submit_enabled
        self.model = model

    def open(self, url: str) -> PageView:
        raise NotImplementedError(_MSG)

    def current_url(self) -> str:
        raise NotImplementedError(_MSG)

    def page_text(self) -> str:
        raise NotImplementedError(_MSG)

    def page_dom(self) -> str:
        raise NotImplementedError(_MSG)

    def query(self, selector: str) -> list[str]:
        raise NotImplementedError(_MSG)

    def needs_human(self) -> bool:
        raise NotImplementedError(_MSG)

    def type_text(self, selector: str, text: str) -> None:
        if not self.submit_enabled:
            raise PermissionError("This CUA controller is read-only.")
        raise NotImplementedError(_MSG)

    def click(self, selector: str) -> None:
        if not self.submit_enabled:
            raise PermissionError("This CUA controller is read-only.")
        raise NotImplementedError(_MSG)

    def submit(self) -> None:
        if not self.submit_enabled:
            raise PermissionError("This CUA controller is read-only.")
        raise NotImplementedError(_MSG)
