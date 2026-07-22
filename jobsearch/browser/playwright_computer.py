"""Playwright-backed Computer + browser primitives (real Chromium).

Two roles:

* ``PlaywrightComputer`` implements the ``Computer`` interface consumed by
  ``OpenAIComputerUseController`` — it executes actions (coordinate-based from the
  OpenAI computer-use model, or selector-based from our own controller) and
  returns PNG screenshots. Use this when ``computer-use-preview`` is available.
* It also exposes DOM helpers (``page_text``, ``form_fields``, ``has_human_gate``)
  used by the DOM-native ``PlaywrightBrowserController`` — the reliable path for
  reading/filling real application forms without needing a vision/CU model.

Sandbox note: if the browser's system libs were installed into a userland prefix
(``~/.local/pw-libs``) because root/apt wasn't available, ``_ensure_lib_path``
wires ``LD_LIBRARY_PATH`` before launch so Chromium starts. On a normal host with
``playwright install-deps`` run, that prefix simply won't exist and this is a
no-op.
"""

from __future__ import annotations

import os
from typing import Any, Optional

# JS that extracts fillable form fields with a usable CSS selector for each.
_FORM_FIELDS_JS = r"""
() => {
  const out = [], seen = new Set();
  const skip = ['hidden','submit','button','reset','image','file'];
  document.querySelectorAll('input, textarea, select').forEach(el => {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    const isFile = (el.getAttribute('type')||'').toLowerCase() === 'file';
    if (skip.includes(type) && !isFile) return;
    const nameAttr = el.getAttribute('name') || '';
    let selector = '';
    if (nameAttr) selector = `[name="${nameAttr}"]`;
    else if (el.id) selector = `#${el.id}`;
    if (!selector || seen.has(selector)) return;
    seen.add(selector);
    let label = '';
    if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) label = (l.innerText||'').trim(); }
    if (!label) label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || nameAttr;
    out.push({
      name: nameAttr || label,
      label: label,
      type: isFile ? 'file' : type,
      required: !!el.required,
      selector: selector,
    });
  });
  return out;
}
"""

_HUMAN_GATE_JS = r"""
() => {
  const html = document.documentElement.innerHTML.toLowerCase();
  const captcha = !!document.querySelector(
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha, .h-captcha, [data-sitekey]');
  // Login wall heuristic: a password field with sign-in framing.
  const pw = !!document.querySelector('input[type="password"]');
  const loginy = /(sign in|log in|login|password)/.test(html) && pw
                 && document.querySelectorAll('input').length <= 4;
  return captcha || loginy;
}
"""


def _ensure_lib_path() -> None:
    d = os.path.expanduser("~/.local/pw-libs/root/usr/lib/x86_64-linux-gnu")
    if os.path.isdir(d):
        cur = os.environ.get("LD_LIBRARY_PATH", "")
        if d not in cur.split(":"):
            os.environ["LD_LIBRARY_PATH"] = d + (":" + cur if cur else "")


class PlaywrightComputer:
    """Drives a real headless Chromium. Implements the Computer interface."""

    def __init__(self, *, headless: bool = True, width: int = 1280, height: int = 800):
        _ensure_lib_path()
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._page = self._browser.new_page(viewport={"width": width, "height": height})
        self._w, self._h = width, height

    # -- Computer interface -------------------------------------------- #
    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def current_url(self) -> str:
        return self._page.url

    def screenshot(self) -> bytes:
        return self._page.screenshot()

    def dimensions(self) -> tuple[int, int]:
        return (self._w, self._h)

    def perform(self, action: dict[str, Any]) -> None:
        """Execute one action. Supports OpenAI computer-use coordinate actions
        and our own selector-based actions (type/click/submit)."""
        kind = action.get("type", "")
        page = self._page
        if kind == "type":
            sel = action.get("selector")
            text = action.get("text", "")
            if sel:
                el = page.query_selector(sel)
                if el and (el.get_attribute("type") or "").lower() == "file":
                    # Résumé/cover upload: attach the file rather than typing.
                    if text and os.path.exists(text):
                        page.set_input_files(sel, text)
                else:
                    page.fill(sel, text)
            else:
                page.keyboard.type(text)
        elif kind == "click":
            sel = action.get("selector")
            if sel:
                page.click(sel)
            else:
                page.mouse.click(action.get("x", 0), action.get("y", 0),
                                 button=action.get("button", "left"))
        elif kind == "double_click":
            sel = action.get("selector")
            if sel:
                page.dblclick(sel)
            else:
                page.mouse.dblclick(action.get("x", 0), action.get("y", 0))
        elif kind == "keypress":
            for k in action.get("keys", []) or ([action["key"]] if action.get("key") else []):
                page.keyboard.press(k)
        elif kind == "scroll":
            page.mouse.wheel(action.get("scroll_x", 0), action.get("scroll_y", 0))
        elif kind == "move":
            page.mouse.move(action.get("x", 0), action.get("y", 0))
        elif kind == "wait":
            page.wait_for_timeout(int(action.get("ms", 500)))
        elif kind == "submit":
            self._submit_form()
        # unknown action types are ignored (screenshot handled by caller)

    def _submit_form(self) -> None:
        for sel in ("button[type=submit]", "input[type=submit]",
                    "button:has-text('Submit')", "button:has-text('Apply')"):
            loc = self._page.locator(sel)
            if loc.count() > 0:
                loc.first.click()
                return
        self._page.keyboard.press("Enter")  # fallback

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    # -- DOM helpers (used by PlaywrightBrowserController) --------------- #
    def page_text(self) -> str:
        try:
            return self._page.inner_text("body")
        except Exception:
            return ""

    def page_html(self) -> str:
        return self._page.content()

    def form_fields(self) -> list[dict]:
        try:
            return list(self._page.evaluate(_FORM_FIELDS_JS))
        except Exception:
            return []

    def has_human_gate(self) -> bool:
        try:
            return bool(self._page.evaluate(_HUMAN_GATE_JS))
        except Exception:
            return False
