"""OpenAI computer-use BrowserController (spec extension: browser control by OpenAI).

Implements the ``BrowserController`` interface on top of two OpenAI capabilities:

* a **vision** model (e.g. gpt-4o) reads the current screenshot for
  ``page_text`` / ``query`` extraction (READ_BROWSER, side-effect free);
* the **computer-use** model plans mutating actions (click / type / submit),
  executed only when ``submit_enabled`` is True (GATED).

Trust-tier enforcement lives here too: in read-only mode the controller executes
ONLY non-mutating actions (navigate-by-URL, scroll, wait, screenshot). It refuses
to type, press keys, or click — so a READ_BROWSER unit can never fill or submit a
form even through the model. The actual browser is abstracted behind the
``Computer`` backend (Playwright/Chromium in production, ``MockComputer`` in tests).
"""

from __future__ import annotations

import abc
import base64
import json
from typing import Any, Optional

from .controller import BrowserController, PageView


# --------------------------------------------------------------------------- #
# Computer backend abstraction (the real browser lives behind this).
# --------------------------------------------------------------------------- #
class Computer(abc.ABC):
    @abc.abstractmethod
    def goto(self, url: str) -> None: ...
    @abc.abstractmethod
    def current_url(self) -> str: ...
    @abc.abstractmethod
    def screenshot(self) -> bytes:
        """Return a PNG screenshot of the current viewport."""
    @abc.abstractmethod
    def dimensions(self) -> tuple[int, int]: ...
    @abc.abstractmethod
    def perform(self, action: dict[str, Any]) -> None:
        """Execute one low-level action (click/type/scroll/keypress/...)."""


# Action types that mutate the page — forbidden in read-only mode.
_MUTATING = {"click", "type", "keypress", "double_click", "drag", "scroll_into_type"}


class OpenAIComputerUseController(BrowserController):
    def __init__(
        self,
        computer: Computer,
        *,
        submit_enabled: bool = False,
        vision_model: str = "gpt-4o-mini",
        computer_model: str = "computer-use-preview",
        api_key: str | None = None,
        max_steps: int = 8,
        client: Any = None,
    ):
        # `client` is injectable so the read-only enforcement can be tested
        # offline without network. In production it is built from the API key.
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI
            import os
            key = api_key or os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("OPENAI_API_KEY is not set.")
            self._client = OpenAI(api_key=key)
        self.computer = computer
        self.submit_enabled = submit_enabled
        self.vision_model = vision_model
        self.computer_model = computer_model
        self.max_steps = max_steps

    # -- helpers -------------------------------------------------------- #
    def _screenshot_b64(self) -> str:
        return base64.b64encode(self.computer.screenshot()).decode("ascii")

    def _vision(self, instruction: str) -> str:
        """Read the current screenshot with the vision model (no side effects)."""
        b64 = self._screenshot_b64()
        resp = self._client.chat.completions.create(
            model=self.vision_model,
            temperature=0.0,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
        )
        return (resp.choices[0].message.content or "").strip()

    # -- read operations ------------------------------------------------ #
    def open(self, url: str) -> PageView:
        self.computer.goto(url)  # navigation by URL is read-only-safe
        text = self.page_text()
        return PageView(url=self.computer.current_url(), text=text)

    def current_url(self) -> str:
        return self.computer.current_url()

    def page_text(self) -> str:
        return self._vision(
            "Transcribe all visible text on this page. Output plain text only."
        )

    def page_dom(self) -> str:
        # Vision backend has no DOM; return the transcribed text as a proxy.
        return self.page_text()

    def query(self, selector: str) -> list[str]:
        """Extract items described by ``selector`` as a JSON array of strings."""
        out = self._vision(
            f"From this page, extract every item matching: {selector!r}. "
            "Respond ONLY with a JSON array of strings (one per item)."
        )
        try:
            data = json.loads(out[out.find("["): out.rfind("]") + 1])
            return [str(x) for x in data] if isinstance(data, list) else []
        except Exception:
            return []

    def needs_human(self) -> bool:
        ans = self._vision(
            "Does this page require login, a password, or a CAPTCHA to proceed? "
            "Answer strictly 'yes' or 'no'."
        ).lower()
        return ans.startswith("y")

    # -- action operations (GATED) ------------------------------------- #
    def _guard_mutation(self, kind: str) -> None:
        if not self.submit_enabled:
            raise PermissionError(
                f"Read-only controller refused a mutating action: {kind}."
            )

    def _run_computer_use(self, goal: str) -> list[dict]:
        """Run the computer-use loop toward ``goal``; return executed actions.

        Every proposed action is filtered: mutating actions require
        submit_enabled. This is the safety choke point for the GATED tier.
        """
        w, h = self.computer.dimensions()
        tools = [{"type": "computer_use_preview", "display_width": w,
                  "display_height": h, "environment": "browser"}]
        b64 = self._screenshot_b64()
        resp = self._client.responses.create(
            model=self.computer_model,
            tools=tools,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": goal},
                    {"type": "input_image",
                     "image_url": f"data:image/png;base64,{b64}"},
                ],
            }],
            truncation="auto",
        )
        executed: list[dict] = []
        for _ in range(self.max_steps):
            calls = [it for it in resp.output if getattr(it, "type", "") == "computer_call"]
            if not calls:
                break
            call = calls[0]
            action = call.action.model_dump() if hasattr(call.action, "model_dump") else dict(call.action)
            kind = action.get("type", "")
            if kind in _MUTATING:
                self._guard_mutation(kind)  # raises in read-only mode
            self.computer.perform(action)
            executed.append(action)
            new_b64 = self._screenshot_b64()
            resp = self._client.responses.create(
                model=self.computer_model,
                tools=tools,
                previous_response_id=resp.id,
                input=[{
                    "type": "computer_call_output",
                    "call_id": call.call_id,
                    "output": {"type": "computer_screenshot",
                               "image_url": f"data:image/png;base64,{new_b64}"},
                }],
                truncation="auto",
            )
        return executed

    def type_text(self, selector: str, text: str) -> None:
        self._guard_mutation("type")
        self.computer.perform({"type": "type", "text": text, "selector": selector})

    def click(self, selector: str) -> None:
        self._guard_mutation("click")
        self.computer.perform({"type": "click", "selector": selector})

    def submit(self) -> None:
        self._guard_mutation("submit")
        self.computer.perform({"type": "submit"})
