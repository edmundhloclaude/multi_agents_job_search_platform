"""BrowserController interface (spec §1).

The rest of the system depends only on this abstraction, never on CUA
(Claude-in-Chrome) specifics, so it can be mocked in tests.

Capability model — this is how trust tiers are enforced at the browser layer:

* Read operations (``open``, ``current_url``, ``page_text``, ``page_dom``,
  ``query``, ``needs_human``) are always available.
* Mutating operations (``type_text``, ``click``, ``submit``) exist only on a
  controller whose ``submit_enabled`` is True.
* ``ReadOnlyBrowser`` wraps any controller and hard-disables the mutating ops.
  The orchestrator hands READ_BROWSER units a ``ReadOnlyBrowser`` so a
  read-only unit can never physically perform a write, even if it tries.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


class HumanHandoffRequired(Exception):
    """Raised when a page needs login / CAPTCHA / credentials.

    No agent handles credentials or CAPTCHAs (spec §0.5). The unit pauses and
    hands control to the human.
    """


@dataclass
class PageView:
    """A read-only snapshot of the current page."""
    url: str
    text: str = ""
    dom: str = ""
    links: list[str] = field(default_factory=list)
    requires_login: bool = False
    requires_captcha: bool = False


class BrowserController(abc.ABC):
    """Abstract browser control. Read methods everywhere; write methods gated."""

    #: Whether this controller can perform irreversible mutating actions.
    #: A read-only controller (or ReadOnlyBrowser wrapper) reports False.
    submit_enabled: bool = False

    # -- read-only operations ------------------------------------------- #
    @abc.abstractmethod
    def open(self, url: str) -> PageView: ...

    @abc.abstractmethod
    def current_url(self) -> str: ...

    @abc.abstractmethod
    def page_text(self) -> str: ...

    @abc.abstractmethod
    def page_dom(self) -> str: ...

    @abc.abstractmethod
    def query(self, selector: str) -> list[str]:
        """Return text/values of elements matching a selector."""

    def needs_human(self) -> bool:
        """True if the current page needs login or CAPTCHA (spec §0.5)."""
        return False

    # -- mutating operations (only valid when submit_enabled) ----------- #
    def type_text(self, selector: str, text: str) -> None:
        raise PermissionError("This controller is read-only; type_text is not permitted.")

    def click(self, selector: str) -> None:
        raise PermissionError("This controller is read-only; click is not permitted.")

    def submit(self) -> None:
        raise PermissionError("This controller is read-only; submit is not permitted.")

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class ReadOnlyBrowser(BrowserController):
    """Read-only proxy over any BrowserController (spec §0 READ_BROWSER tier).

    Delegates read operations to the wrapped controller; every mutating
    operation raises ``PermissionError``. The orchestrator gives this to the
    Crawler so a READ_BROWSER unit can never submit, click, or fill — enforced,
    not merely by convention.
    """

    submit_enabled = False

    def __init__(self, inner: BrowserController):
        self._inner = inner

    def open(self, url: str) -> PageView:
        return self._inner.open(url)

    def current_url(self) -> str:
        return self._inner.current_url()

    def page_text(self) -> str:
        return self._inner.page_text()

    def page_dom(self) -> str:
        return self._inner.page_dom()

    def query(self, selector: str) -> list[str]:
        return self._inner.query(selector)

    def needs_human(self) -> bool:
        return self._inner.needs_human()

    # mutating ops inherit the raising defaults from BrowserController.

    def close(self) -> None:  # pragma: no cover - trivial
        self._inner.close()
