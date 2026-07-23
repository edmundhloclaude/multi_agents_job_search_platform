# Multi-Agent Job Search Platform

A multi-agent system that finds, screens, tailors for, and (with a mandatory
human gate) applies to jobs. Five working units plus an orchestrator and a
shared job store. **Trust tiers are enforced by the orchestrator, and no
application is ever submitted without explicit typed human approval.**

---

## Quickstart (run it with your own job search)

```bash
git clone <your-fork-url> && cd multi_agents_job_search_platform
python -m venv .venv && source .venv/bin/activate     # or: uv venv .venv
pip install -r requirements.txt                        # or: uv pip install -r requirements.txt

# 1) (optional) API keys — only needed for GPT reasoning / live sources
cp .env.example .env            # then add OPENAI_API_KEY / THEIRSTACK_API_KEY

# 2) your settings — aspirations, sources, models
cp jobsearch/config.example.yaml jobsearch/config.yaml   # edit the `profile:` block

# 3) your real experience (the ONLY source of truth for résumé claims)
cp jobsearch/accomplishment_bank.example.yaml jobsearch/accomplishment_bank.yaml
#    then edit it, and point `paths.accomplishment_bank` in config.yaml at it

# 4) run — halts at the human submit gate
python -m jobsearch run
python -m jobsearch status -v
```

With **zero setup** (no keys, defaults `llm.provider: none` + `browser.driver: mock`)
a fresh clone runs the full pipeline offline against bundled fixtures — so you can
see the whole workflow before wiring anything real. `config.yaml`,
`accomplishment_bank.yaml`, `.env`, and `data/` are git-ignored: **your
aspirations, résumé, keys, and results never get committed.** To go live, set
`llm.provider: openai`, enable a source (e.g. `theirstack`) with its key in `.env`,
and optionally `browser.driver: playwright`.

Prefer to define your strategy conversationally? `python -m jobsearch strategy-web`
(chat + résumé upload → criteria).

---

## Core safety design (non-negotiable)

| Guarantee | Where it lives |
|---|---|
| **Trust tiers enforced, not by convention** | `orchestrator.assert_controller_for_tier` — a `READ_BROWSER` unit can never receive a submit-capable controller; a `SAFE` unit gets no browser at all. |
| **Mandatory submit gate, no bypass** | `orchestrator.human_approval_gate` is the *only* code that can mint an approved `Approval` (guarded by a module-private token). `Applier.submit` refuses to act without one. There is no auto-approve flag and no env-var bypass. |
| **Crafter never fabricates** | `crafter.find_fabrications` re-inspects generated docs; any employer/title/date/metric/skill not traceable to the accomplishment bank aborts emission. |
| **No credentials handled by any agent** | `applier` flags password/SSN/payment/CAPTCHA fields as *sensitive* and hands them to the human — they are never auto-filled. Login walls/CAPTCHAs during crawling trigger a human handoff. |
| **ToS posture** | Per-source `enabled` flag + per-source rate limiting (conservative default), official APIs/feeds preferred over scraping, every posting's source logged. |

### Trust tiers

- **SAFE** — pure reasoning, no browser: Strategy, Screener, Crafter, and the
  Applier *form-mapping* step.
- **READ_BROWSER** — drives the browser read-only, extracts data, never clicks
  submit/apply and never fills forms: Crawler.
- **GATED** — the single irreversible browser action: the Applier *submit* step,
  reachable only through the human approval gate.

---

## Setup

Requires Python 3.11+.

```bash
cd multi_agents_job_search_platform
python -m venv .venv && source .venv/bin/activate     # or: uv venv .venv
pip install -r requirements.txt                        # or: uv pip install -r requirements.txt
```

Run any command as a module:

```bash
python -m jobsearch <command> [-c path/to/config.yaml]
```

The schema (`jobsearch/store/schema.sql`) is applied automatically on first run
— the SQLite file is created empty and migrated in place.

---

## Configuration (`jobsearch/config.yaml`)

Operational settings only. Copy `config.example.yaml` → `config.yaml` (git-ignored).

- `paths` — db, output dir, `strategy.md`, and the accomplishment bank
  (relative paths resolve against the config file's directory).
- `llm` — reasoning backend: `provider: none` (offline) or `openai` (+ model).
- `browser.driver` — `mock` (offline fixtures), `playwright` (real Chromium), or
  `cua` (Claude computer-use stub).
- `profile` — the Strategy Advisor's input (aspirations, comp band, must-haves,
  dealbreakers…). *No identity here* — see below.
- `sources` — each with `enabled`, `rate_limit_per_min`, `type`
  (`api`/`browser`/`mcp`); secrets referenced by env-var name, never inline.

**Your identity is not in config.** Name and contact live in the **accomplishment
bank** (`name` / `contact`) — the Crafter uses them for résumés and the Applier for
form-filling. The Strategy Advisor never receives identity. (Never put passwords /
payment / SSN anywhere — no agent handles those.)

---

## Accomplishment bank format (`accomplishment_bank.example.yaml`)

The **only** source of truth for claims. The Crafter selects and rewords from
it; it never invents anything, and the fabrication check enforces that.

```yaml
name: "Jane Doe"
contact: { email: ..., phone: ..., linkedin: ..., website: ... }
accomplishments:
  - employer: "Acme Corp"
    title: "Senior Engineer"
    start_date: "2019"
    end_date: "2023"
    text: "Built a distributed system in Python that cut p99 latency by 40%..."
    metrics: ["40%", "2M"]        # every metric that appears in output must be here
    skills: ["python", "distributed systems", "kubernetes"]
skills: [ ... ]                    # global skills
credentials: [ ... ]
```

If a generated document contains a metric, employer, title, date, or skill not
present in this file, the Crafter **refuses to emit it** (`FabricationError`).

---

## Full workflow

```
strategy → crawl → screen → craft (screened_in) → apply-map → [HUMAN GATE] → apply-submit
```

Each stage is independently runnable and idempotent, so a failure in one never
forces re-running the others.

```bash
python -m jobsearch strategy      # build strategy.md + machine-usable criteria
python -m jobsearch crawl         # extract postings (all enabled sources)
python -m jobsearch crawl --source theirstack,fantastic_jobs   # only these vendors
python -m jobsearch crawl --list-sources                       # list configured sources
python -m jobsearch screen        # score 0–100, screen_in / screen_out
python -m jobsearch craft         # tailor resume + cover letter for screened_in jobs
python -m jobsearch apply-map     # SAFE: map each form to your data -> awaiting_approval
python -m jobsearch apply-submit  # GATED: review + type SUBMIT to submit, one job at a time
python -m jobsearch status -v     # summary table + postings + run log
python -m jobsearch reset         # clear the store (--yes to skip prompt, --all also wipes docs/strategy)
```

Or the whole thing at once, **which deliberately halts before the gate**:

```bash
python -m jobsearch run           # runs strategy…apply-map, then stops
python -m jobsearch apply-submit  # the human gate, run separately
```

### Where the human gate fires

`apply-submit` processes **one application at a time**. For each, it prints the
fully-filled application — every field, the resume/cover paths, and any
sensitive fields that will *not* be auto-filled — then blocks on:

```
To SUBMIT this application, type exactly: SUBMIT
Anything else (including empty) will SKIP it. No auto-approve exists.
Confirm >
```

Only the exact string `SUBMIT` proceeds; the browser keystrokes + submit click
happen *after* that, and never for credential/payment/CAPTCHA fields. Anything
else marks the job `skipped`. There is no configuration, flag, or environment
variable that bypasses this.

---

## The job store (`jobsearch/store/`)

SQLite, single file, wrapped by the `JobStore` repository — all agents read and
write only through it. The primary key is `dedup_key`
(`normalized company|title|location`, lowercased / punctuation-stripped /
whitespace-collapsed), so re-crawling automatically skips already-seen postings
via `upsert_posting` without clobbering screening/application state.

---

## Bank-grounded strategy criteria

When `llm.provider: openai`, `jobsearch strategy` derives the machine-usable
screening criteria (target roles, seniority, must-haves, boost keywords) from the
candidate's **real accomplishment bank** + the aspirations in `config.yaml`'s
`profile`, instead of copying the profile verbatim. Guardrails:

- **Hard constraints stay deterministic** from the profile — comp floor, geography,
  remote, dealbreakers. The LLM cannot override them.
- **Grounding guard**: a proposed `must_have` not traceable to the bank's
  demonstrated skills or your stated aspirations is **dropped** (an ungrounded
  must-have over-filters screening); dropped items are listed in `strategy.md`.
- **Graceful fallback**: no LLM/bank, or any error → the deterministic profile
  criteria. The `strategy.md` "How these criteria were derived" section records the
  source, rationale, and anything dropped.

## Strategy ↔ Crafter collaboration

The Strategy Advisor and Crafter share the accomplishment bank (truth) and the
strategy (lens), and hand off in both directions:

- **Positioning → tailoring.** `strategy` emits a `positioning` block (narrative,
  lead-with themes, emphasize/de-emphasize) into `strategy.md`. The Crafter reads it
  and biases which real accomplishments it foregrounds and how it frames the cover
  letter — so every document is on-brand, not just keyword-matched. (Fabrication
  guard unchanged: emphasis only, never new claims.)
- **Gap feedback.** `jobsearch gaps` (also a section in `strategy.md`) reports skills
  your targets want that the bank can't back — `missing` (no evidence) vs `weak`
  (one story / listed-only) — so you can retarget or add evidence.
- **Shared intake → bank.** In `strategy-web`, upload a résumé and click *Draft bank
  entries*: the advisor extracts structured accomplishments for your review, and
  *Add to accomplishment_bank.yaml* writes the accepted ones back to the Crafter's
  source of truth (existing identity/entries preserved).

## Interactive Strategy Advisor (chat + documents, web UI)

```bash
python -m jobsearch strategy-web        # http://127.0.0.1:8766  (needs OPENAI_API_KEY)
```

A local web app to author your strategy conversationally:

- **Chat** with the advisor (OpenAI) about your goals; it asks clarifying questions.
- **Upload documents** (résumé / job descriptions / brag doc — `.txt`/`.md`/`.pdf`/
  `.docx`); the advisor reads them and folds them into your criteria.
- **Live YAML preview** of the machine-usable screening criteria as it evolves, with
  a ⚠ flag on any must-have not grounded in your bank/aspirations (advisory — you're
  in control here).
- **Save** writes `strategy.md` in the exact format the Screener consumes.

Localhost-only and scoped to strategy authoring: it writes `strategy.md` on Save and
does nothing else — it can't crawl, screen, or submit.

## Web dashboard (read-only)

Watch the agents work in a browser:

```bash
python -m jobsearch serve            # http://127.0.0.1:8765
```

It reads the same SQLite store the Orchestrator writes to, so it updates live
(polls every 2s) as stages run. Shows status tiles, the pipeline stage strip
(with the active stage highlighted), the Orchestrator's run log with trust-tier
badges (SAFE / READ_BROWSER / GATED), and the jobs table.

It's the **hub**: a "Strategy Advisor →" link opens the interactive advisor at
`/strategy` on the same server/port (with a link back). One `serve`, one URL —
the strategy session is built lazily, so the dashboard still works without OpenAI.
The dashboard's own status routes stay read-only (POST → 405); the `/strategy`
sub-app is the authoring surface that writes `strategy.md` / the bank.

Stdlib `http.server` (no extra deps), bound to localhost, and **strictly
read-only** — GET only; it cannot trigger stages or submit anything. The submit
gate stays a typed CLI confirmation (spec §0.2); a web button must never cross it.

## Real browser apply flow (Playwright)

With `browser.driver: playwright`, `apply-map` and `apply-submit` drive a real
headless Chromium:

- **apply-map** (read-only) opens each application URL and reads the real form
  fields from the DOM (`name`/`type`/`required` + a CSS `selector` per field).
- **apply-submit** (gated) fills those fields in the browser and submits —
  **only after typed human approval** — uploading the résumé file via the file
  input. Sensitive fields (password/payment/CAPTCHA) are never filled; login
  walls/CAPTCHAs set the human-handoff flag.

Setup:
```bash
playwright install chromium
playwright install-deps chromium      # system libs (needs root); see below
```
If you lack root, the libs can be extracted into a userland prefix
(`~/.local/pw-libs`) and the app auto-adds them to `LD_LIBRARY_PATH` at launch.
No Chromium? Set `browser.driver: mock` to fall back to the offline fixtures.

The `OpenAIComputerUseController` can also use the same `PlaywrightComputer`
backend (OpenAI plans actions from screenshots, Playwright executes them) once
`computer-use-preview` is enabled on your key; the DOM-native controller above
is the reliable default and needs no vision/CU model.

## Live API sources (TheirStack, Fantastic.jobs)

Add a `type: api` source with a known `provider`; the key lives in `.env`
(referenced by `api_key_env`), never in config. Bundled providers:

- **`theirstack`** — TheirStack aggregator (LinkedIn/Indeed/Glassdoor/ATS), deduped.
- **`fantastic_jobs`** — Fantastic.jobs Active Jobs DB (200k+ career sites + ATS +
  boards, hourly). Supports the direct API (`auth: bearer`, default) or RapidAPI
  (`auth: rapidapi`). `time_frame`/`title`/`location`/`limit` params; the posting
  description feeds the Screener.

Adding another provider is a ~60-line fetcher `(source, query) -> list[dict]`
registered in `orchestrator._build_api_fetchers` — see `jobsearch/sources/`.

### TheirStack

The Crawler ships with a real aggregator source, **TheirStack** (covers
LinkedIn/Indeed/Glassdoor/ATS, deduped). Enable it in three steps:

1. Get a key at https://app.theirstack.com → Settings → API keys.
2. Put it in a project-local **`.env`** (git-ignored, auto-loaded by the CLI):
   ```
   THEIRSTACK_API_KEY=your-key
   ```
3. The `theirstack` source in `config.yaml` (`type: api`, `provider: theirstack`)
   is enabled by default; run `python -m jobsearch crawl`.

Credits are consumed per result — keep `params.limit` and `max_pages` small
(the shipped default is `limit: 5`, one page). The source maps TheirStack's
`technology_slugs`/`keyword_slugs` into `requirements` and its `description`
into `raw`, so the Screener has real signal to score against. Keys live in
`.env`, never in `config.yaml`; a real environment variable always overrides
`.env`.

## MCP-backed job sources (Crawler)

The Crawler can act as an **MCP client**, fetching postings from an external MCP
server instead of scraping — the preferred, ToS-friendly path (spec §0.6). Add a
source with `type: mcp`:

```yaml
- name: indeed
  enabled: true
  type: mcp
  mcp:
    transport: stdio                 # or "http"
    command: python                  # stdio: how to launch the server
    args: ["-m", "indeed_mcp_server"]
    # url: "https://your-indeed-mcp/mcp"   # for transport: http
    tool: search_jobs                # MUST be read-only (guard enforced)
    query_arg: query
    params: { location: "Remote", limit: 25 }
    result_path: jobs                # dot-path to the job list in the result
    field_map: { company: company, title: title, location: location,
                 url: url, comp_text: salary, application_method: application_method }
```

**Read-only guard.** The MCP source refuses to call any tool that isn't provably
read-only: a tool whose *name* looks mutating (`apply`/`submit`/`create`/…) is
rejected before the session even opens, and if the server advertises MCP
annotations, `destructiveHint`/`readOnlyHint` are honored. So the Crawler keeps
its READ_BROWSER posture — it can never trigger an application through MCP.

**Indeed note.** No Indeed MCP ships here (and Indeed closed its public job API
and blocks scraping). Point `command`/`url` at a real server backed by a
*licensed* data provider. `tests/fixtures/fake_indeed_mcp_server.py` is a working
example server used by the test suite (`tests/test_mcp_crawler.py` drives it over
real stdio).

## Project layout

```
jobsearch/
  orchestrator.py     # workflow control, tier enforcement, submit gate
  store/
    schema.sql        # applied on first run
    job_store.py      # repository over SQLite; dedup lives here
  agents/
    strategy.py       # SAFE
    crawler.py        # READ_BROWSER
    screener.py       # SAFE
    crafter.py        # SAFE (+ fabrication check)
    applier.py        # SAFE map step + GATED submit step
  browser/
    controller.py     # BrowserController interface + ReadOnlyBrowser wrapper
    cua_controller.py # Claude-in-Chrome implementation (stub)
    mock_controller.py# for tests / offline demo
  models.py           # shared dataclasses
  cli.py              # argparse CLI
  config.example.yaml # template → copy to config.yaml (paths, llm, browser, sources, profile)
  accomplishment_bank.example.yaml
  mock_fixtures.json  # offline pages for browser.driver=mock
tests/
requirements.txt
```

---

## Tests

```bash
python -m pytest -q
```

Covers: `dedup_key` normalization + collision handling, Screener idempotency,
the Crafter fabrication check (rejects a planted false claim), and the submit
gate (submit is unreachable without a valid approval return value — including
declined, forged, and reused-approval attacks). All tests use
`mock_controller.py`; none touch a real browser.

---

## Wiring the real browser

`cua_controller.py` is a documented stub. To go live, implement its read
methods via Claude-in-Chrome computer-use screen/DOM reads and its mutating
methods (guarded by `submit_enabled`), then set `browser.driver: cua`. The
orchestrator only ever constructs a submit-capable controller *inside* the gate
flow; `READ_BROWSER` units always get a `ReadOnlyBrowser` wrapper.
```
