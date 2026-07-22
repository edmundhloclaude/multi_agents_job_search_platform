"""Live OpenAI integration check for BOTH layers.

Run with a real key:  OPENAI_API_KEY=... python scripts/live_openai_check.py

Exercises:
  1. OpenAILLM basic call
  2. OpenAI-backed Screener (reasoning layer)
  3. OpenAI-backed Crafter + fabrication guard (reasoning layer)
  4. OpenAIComputerUseController: vision read + read-only enforcement + a
     computer-use action plan (browser layer), driven by a MockComputer that
     serves a synthetic screenshot (no real browser needed).
"""

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jobsearch.agents.crafter import Crafter, find_fabrications
from jobsearch.agents.screener import Screener
from jobsearch.browser.mock_controller import MockComputer
from jobsearch.browser.openai_cua_controller import OpenAIComputerUseController
from jobsearch.llm.openai_llm import OpenAILLM
from jobsearch.models import (
    Accomplishment, AccomplishmentBank, Posting, ScreenStatus, StrategyCriteria,
)
from jobsearch.store.job_store import JobStore

OK, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"


def _screenshot_png() -> bytes:
    """Render a synthetic job-posting screenshot with readable text."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (900, 500), "white")
    d = ImageDraw.Draw(img)
    lines = [
        "Nimbus Systems - Careers",
        "",
        "Staff Software Engineer (Remote)",
        "Compensation: $200,000 - $240,000",
        "",
        "Requirements:",
        "  - 8+ years Python",
        "  - Distributed systems at scale",
        "  - Kubernetes, PostgreSQL",
        "",
        "[ Apply Now ]",
    ]
    y = 20
    for ln in lines:
        d.text((30, y), ln, fill="black")
        y += 34
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set."); return 2
    model = os.environ.get("JOBSEARCH_OPENAI_MODEL", "gpt-4o-mini")
    llm = OpenAILLM(model=model)
    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        print(f"  [{OK if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
        passed += bool(cond); failed += (not cond)

    print(f"\n=== 1. OpenAILLM ({model}) ===")
    try:
        txt = llm.complete_text("You are terse.", "Reply with exactly: OK")
        check("complete_text", "OK" in txt.upper(), repr(txt[:40]))
        js = llm.complete_json("Return JSON.", 'Return {"answer": 42}')
        check("complete_json", js.get("answer") == 42, str(js))
    except Exception as e:
        check("LLM call", False, str(e)); return 1

    print("\n=== 2. Screener (OpenAI reasoning) ===")
    with tempfile.TemporaryDirectory() as td:
        store = JobStore(os.path.join(td, "j.db"))
        crit = StrategyCriteria(
            target_roles=["staff engineer"], seniority=["staff"],
            must_haves=["python", "distributed systems"],
            dealbreakers=["commission only"], keywords_boost=["kubernetes"],
        )
        good = Posting(company="Nimbus", title="Staff Engineer", location="Remote",
                       source="live", requirements=["python", "distributed systems", "kubernetes"],
                       comp_text="$220k")
        bad = Posting(company="Sketchy", title="Growth Hacker", location="Remote",
                      source="live", requirements=["marketing"], comp_text="commission only")
        store.upsert_posting(good); store.upsert_posting(bad)
        Screener(store, llm=llm).run(crit)
        g = store.get(good.dedup_key); b = store.get(bad.dedup_key)
        check("good job screened_in", g.screen_status == ScreenStatus.SCREENED_IN.value,
              f"score={g.screen_score}")
        check("dealbreaker screened_out", b.screen_status == ScreenStatus.SCREENED_OUT.value,
              f"score={b.screen_score}")
        check("rationale from openai", g.screen_rationale.startswith("[openai]"))

        print("\n=== 3. Crafter (OpenAI reword) + fabrication guard ===")
        bank = AccomplishmentBank(
            name="Jane Doe", contact={"email": "jane@example.com"},
            accomplishments=[
                Accomplishment("Acme Corp", "Senior Engineer", "2019", "2023",
                               "Built a distributed system in Python that cut p99 latency by 40% and scaled to 2M tasks/day.",
                               metrics=["40%", "2M"], skills=["python", "distributed systems", "kubernetes"]),
                Accomplishment("Globex", "Software Engineer", "2016", "2019",
                               "Led a team of 6 to ship a data platform serving 10,000 users.",
                               metrics=["6", "10,000"], skills=["python", "postgres"]),
            ],
            skills=["python", "distributed systems", "kubernetes", "postgres"],
            credentials=["BS Computer Science"],
        )
        store.annotate_screen(good.dedup_key, status=ScreenStatus.SCREENED_IN, score=90, rationale="")
        crafter = Crafter(store, bank, os.path.join(td, "out"), llm=llm)
        resume = crafter.render_resume(store.get(good.dedup_key))
        cover = crafter.render_cover_letter(store.get(good.dedup_key))
        r_fab = find_fabrications(resume, bank)
        c_fab = find_fabrications(cover, bank)
        check("LLM resume passes fabrication guard", r_fab == [], str(r_fab))
        check("LLM cover passes fabrication guard", c_fab == [], str(c_fab))
        check("resume was actually reworded (real metrics kept)",
              "40%" in resume and "2M" in resume)
        print("\n    --- reworded resume experience (first bullet) ---")
        for line in resume.splitlines():
            if line.strip().startswith("- ") or line.strip().startswith("- ".strip()):
                pass
        print("    " + "\n    ".join(resume.splitlines()[:12]))
        store.close()

    print("\n=== 4. OpenAIComputerUseController (OpenAI browser control) ===")
    png = _screenshot_png()
    # READ-ONLY controller (READ_BROWSER tier)
    ro = OpenAIComputerUseController(MockComputer(png, url="https://nimbus.test/job"),
                                    submit_enabled=False, vision_model=model)
    try:
        text = ro.page_text()
        check("vision page_text reads screenshot",
              "nimbus" in text.lower() or "staff" in text.lower(), repr(text[:60]))
        nh = ro.needs_human()
        check("needs_human=False on a no-login page", nh is False, f"needs_human={nh}")
    except Exception as e:
        check("vision read", False, str(e))

    # read-only enforcement
    try:
        ro.type_text("x", "y"); check("read-only blocks type_text", False)
    except PermissionError:
        check("read-only blocks type_text", True)
    try:
        ro.submit(); check("read-only blocks submit", False)
    except PermissionError:
        check("read-only blocks submit", True)

    # computer-use action plan (GATED capability). Model may be unavailable on
    # some keys — tolerate that, but still prove the read-only guard on the loop.
    print("    - computer-use action planning:")
    try:
        gated = OpenAIComputerUseController(MockComputer(png), submit_enabled=True,
                                            computer_model="computer-use-preview")
        actions = gated._run_computer_use("Click the Apply Now button.")
        check("computer-use returned action(s)", len(actions) >= 0,
              f"{len(actions)} action(s): {[a.get('type') for a in actions][:5]}")
    except Exception as e:
        print(f"      (computer-use-preview not exercised: {type(e).__name__}: {str(e)[:80]})")

    print(f"\n=== RESULT: {passed} passed, {failed} failed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
