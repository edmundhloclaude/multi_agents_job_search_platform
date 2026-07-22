"""Read-only web dashboard for the Orchestrator (stdlib http.server, no deps).

Renders what the multi-agent pipeline is doing by reading the SAME SQLite store
the Orchestrator writes to: the run_log (stage + trust tier + message) and the
job statuses. A separate `jobsearch serve` process and a running pipeline share
the store file, so the dashboard updates live as stages execute.

Deliberately READ-ONLY and localhost-only: it cannot trigger stages and cannot
submit anything. The submit gate stays a typed CLI confirmation (spec §0.2);
a web button must never be able to cross it.
"""

from __future__ import annotations

import json
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .store.job_store import JobStore


def build_state(store: JobStore, *, max_jobs: int = 300, max_runs: int = 60) -> dict:
    """Snapshot the store for the dashboard (pure read)."""
    counts = store.status_counts()
    jobs = []
    for p in store.all()[:max_jobs]:
        raw = p.raw if isinstance(p.raw, dict) else {}
        desc = str(raw.get("description", "") or "")[:600]
        jobs.append({
            "dedup_key": p.dedup_key,
            "company": p.company, "title": p.title, "location": p.location,
            "source": p.source, "source_url": p.source_url,
            "comp_text": p.comp_text, "application_method": p.application_method,
            "requirements": p.requirements,
            "screen_status": p.screen_status, "screen_score": p.screen_score,
            "screen_rationale": p.screen_rationale,
            "apply_status": p.apply_status, "response_status": p.response_status,
            "resume_path": p.resume_path, "cover_letter_path": p.cover_letter_path,
            "description": desc,
        })
    return {
        "counts": counts,
        "jobs": jobs,
        "runs": store.recent_runs(max_runs),
        "totals": {
            "postings": len(store.all()),
        },
    }


# --------------------------------------------------------------------------- #
# Dashboard HTML (self-contained; polls /api/state).
# --------------------------------------------------------------------------- #
_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Search — Agent Activity</title>
<style>
  :root { color-scheme: light dark; --bg:#0f1420; --card:#1a2130; --fg:#e7ecf3;
          --muted:#93a1b5; --line:#2a3446; --accent:#5b9dff; }
  @media (prefers-color-scheme: light){ :root{ --bg:#f4f6fb; --card:#fff; --fg:#101828;
          --muted:#5b667a; --line:#e4e8f0; --accent:#2f6fed; } }
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  header{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;
    align-items:center;gap:12px;position:sticky;top:0;background:var(--bg);z-index:5}
  h1{font-size:16px;margin:0;font-weight:650} .dot{width:9px;height:9px;border-radius:50%;
    background:#3ddc84;box-shadow:0 0 0 3px rgba(61,220,132,.2)} .muted{color:var(--muted)}
  main{padding:18px 22px;max-width:1200px;margin:0 auto;display:grid;gap:18px}
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
  .tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
  .tile .n{font-size:22px;font-weight:700} .tile .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .card h2{font-size:13px;margin:0;padding:12px 14px;border-bottom:1px solid var(--line);
    text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
  .stages{display:flex;flex-wrap:wrap;gap:8px;padding:14px}
  .stage{padding:6px 11px;border-radius:20px;border:1px solid var(--line);font-size:12px;color:var(--muted)}
  .stage.done{border-color:#3ddc84;color:var(--fg)} .stage.active{border-color:var(--accent);
    color:var(--fg);box-shadow:0 0 0 2px rgba(91,157,255,.25)}
  table{width:100%;border-collapse:collapse} th,td{text-align:left;padding:9px 14px;
    border-bottom:1px solid var(--line);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:340px}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
  .badge{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid var(--line)}
  .tier-SAFE{color:#3ddc84;border-color:#3ddc84} .tier-READ_BROWSER{color:#5b9dff;border-color:#5b9dff}
  .tier-GATED{color:#ffb454;border-color:#ffb454}
  .s-screened_in,.s-submitted,.s-offer,.s-interview{color:#3ddc84}
  .s-screened_out,.s-skipped,.s-rejected,.s-failed{color:#ff7a90}
  .s-awaiting_approval,.s-drafted{color:#ffb454}
  .log{max-height:320px;overflow:auto} .wrap{overflow-x:auto}
  tr.jobrow{cursor:pointer} tr.jobrow:hover{background:rgba(127,127,127,.10)}
  tr.jobrow td:first-child::before{content:"▸ ";color:var(--muted)}
  tr.jobrow.open td:first-child::before{content:"▾ "}
  tr.detail>td{background:rgba(127,127,127,.06);white-space:normal;max-width:none}
  .dgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px 18px;padding:6px 2px}
  .dgrid .full{grid-column:1/-1} .dgrid b{color:var(--muted);font-weight:600;margin-right:6px}
  .dgrid a{color:var(--accent);word-break:break-all}
  .foot{color:var(--muted);font-size:12px;padding:2px 2px}
</style></head><body>
<header><span class="dot"></span><h1>Job Search — Agent Activity</h1>
  <span class="muted" id="sub"></span>
  <a href="/strategy" style="margin-left:auto;text-decoration:none;padding:6px 12px;border-radius:8px;border:1px solid var(--accent);color:var(--accent);font-weight:600">Strategy Advisor →</a></header>
<main>
  <div class="tiles" id="tiles"></div>
  <div class="card"><h2>Pipeline</h2><div class="stages" id="stages"></div></div>
  <div class="card"><h2>Activity log (Orchestrator run log)</h2>
    <div class="wrap log"><table id="runs"><thead><tr><th>Time</th><th>Stage</th><th>Tier</th><th>Detail</th></tr></thead><tbody></tbody></table></div></div>
  <div class="card"><h2>Jobs</h2>
    <div class="wrap"><table id="jobs"><thead><tr><th>Company</th><th>Title</th><th>Source</th><th>Screen</th><th>Score</th><th>Apply</th><th>Response</th></tr></thead><tbody></tbody></table></div></div>
  <div class="foot" id="foot"></div>
</main>
<script>
const STAGES = ["strategy","crawl","screen","craft","apply-map","apply-submit"];
const esc = s => String(s==null?"":s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
function tiles(c){
  const g=(o)=>Object.entries(o||{}).map(([k,v])=>`<span class="s-${k}">${k} ${v}</span>`).join(" · ")||"—";
  document.getElementById("tiles").innerHTML = [
    ["Postings", state.totals.postings],
    ["Screened", g(c.screen)],["Applications", g(c.apply)],["Responses", g(c.response)],
  ].map(([l,v])=>`<div class="tile"><div class="n" style="font-size:${typeof v==="number"?22:13}px">${v}</div><div class="l">${l}</div></div>`).join("");
}
function stages(runs){
  const started=new Set(), done=new Set();
  runs.slice().reverse().forEach(r=>{ const s=r.stage;
    if((r.message||"").includes("started")) started.add(s); else done.add(s); });
  const last = runs[0] ? runs[0].stage : null;
  const lastRunning = runs[0] && (runs[0].message||"").includes("started");
  document.getElementById("stages").innerHTML = STAGES.map(s=>{
    let cls = done.has(s)?"done":""; if(lastRunning && s===last) cls="active";
    return `<span class="stage ${cls}">${s}</span>`;}).join("");
}
function runsTable(runs){
  document.querySelector("#runs tbody").innerHTML = runs.map(r=>{
    const t=(r.ts||"").replace("T"," ").slice(0,19);
    const tier=r.tier?`<span class="badge tier-${esc(r.tier)}">${esc(r.tier)}</span>`:"";
    return `<tr><td class="muted">${esc(t)}</td><td>${esc(r.stage)}</td><td>${tier}</td><td class="muted">${esc(r.message)}</td></tr>`;
  }).join("");
}
const expanded=new Set();   // dedup_keys whose detail row is open (survives refresh)
function detailHtml(j){
  const reqs=(j.requirements||[]).map(r=>`<span class="badge">${esc(r)}</span>`).join(" ")||"—";
  const url=j.source_url?`<a href="${esc(j.source_url)}" target="_blank" rel="noopener">${esc(j.source_url)}</a>`:"—";
  const docs=[j.resume_path&&"résumé",j.cover_letter_path&&"cover"].filter(Boolean).join(" + ")||"—";
  return `<div class="dgrid">
    <div><b>Location</b>${esc(j.location)||"—"}</div>
    <div><b>Comp</b>${esc(j.comp_text)||"—"}</div>
    <div><b>Method</b>${esc(j.application_method)||"—"}</div>
    <div class="full"><b>URL</b>${url}</div>
    <div class="full"><b>Requirements</b>${reqs}</div>
    <div class="full"><b>Screen</b>${j.screen_score==null?"—":j.screen_score+"/100"} — ${esc(j.screen_rationale)||"(not screened)"}</div>
    <div class="full"><b>Docs</b>${esc(docs)}</div>
    ${j.description?`<div class="full"><b>Description</b><span class="muted">${esc(j.description)}${j.description.length>=600?"…":""}</span></div>`:""}
  </div>`;
}
function jobsTable(js){
  document.querySelector("#jobs tbody").innerHTML = js.map(j=>{
    const open=expanded.has(j.dedup_key);
    return `<tr class="jobrow${open?" open":""}" data-key="${esc(j.dedup_key)}">
      <td>${esc(j.company)}</td><td>${esc(j.title)}</td><td class="muted">${esc(j.source)}</td>
      <td class="s-${esc(j.screen_status)}">${esc(j.screen_status)}</td>
      <td>${j.screen_score==null?"—":j.screen_score}</td>
      <td class="s-${esc(j.apply_status)}">${esc(j.apply_status)}</td>
      <td class="s-${esc(j.response_status)}">${esc(j.response_status)}</td></tr>
      <tr class="detail" data-for="${esc(j.dedup_key)}" style="display:${open?"table-row":"none"}">
        <td colspan="7">${detailHtml(j)}</td></tr>`;
  }).join("");
}
let state={totals:{postings:0},counts:{}};
async function tick(){
  try{
    const r=await fetch("/api/state"); state=await r.json();
    tiles(state.counts); stages(state.runs); runsTable(state.runs); jobsTable(state.jobs);
    document.getElementById("sub").textContent = state.totals.postings+" postings";
    document.getElementById("foot").textContent = "updated "+new Date().toLocaleTimeString();
  }catch(e){ document.getElementById("foot").textContent="disconnected — retrying…"; }
}
// Click a job row to expand/collapse its details (delegated; survives refresh).
document.querySelector("#jobs tbody").addEventListener("click", e=>{
  const tr=e.target.closest("tr.jobrow"); if(!tr) return;
  const key=tr.dataset.key;
  if(expanded.has(key)) expanded.delete(key); else expanded.add(key);
  tr.classList.toggle("open");
  const d=tr.nextElementSibling;
  if(d&&d.classList.contains("detail"))
    d.style.display = (d.style.display==="none"?"table-row":"none");
});
tick(); setInterval(tick, 2000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    db_path = None  # set via partial/subclass

    def log_message(self, *a):  # silence default request logging
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, _HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/state"):
            store = JobStore(self.db_path)   # fresh read connection per request
            try:
                body = json.dumps(build_state(store)).encode("utf-8")
            finally:
                store.close()
            self._send(200, body, "application/json")
        elif self.path == "/strategy" or self.path.startswith("/strategy/"):
            self._strategy("GET")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # The Strategy Advisor sub-app IS a writer (authors strategy.md / bank);
        # the dashboard's own status routes stay strictly read-only.
        if self.path == "/strategy" or self.path.startswith("/strategy/"):
            self._strategy("POST")
        else:
            self._send(405, b"read-only dashboard", "text/plain")

    def _strategy(self, method: str):
        from .strategy_web import route_strategy
        session, err = self.server.strategy_session()
        subpath = self.path[len("/strategy"):] or "/"
        if session is None:
            if method == "GET" and subpath in ("", "/"):
                html = ("<h2>Strategy Advisor unavailable</h2>"
                        f"<p>{err}</p><p><a href='/'>&larr; Dashboard</a></p>")
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(200, json.dumps({"error": err}).encode("utf-8"),
                           "application/json")
            return
        payload = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n:
                try:
                    payload = json.loads(self.rfile.read(n).decode("utf-8"))
                except json.JSONDecodeError:
                    payload = {}
        code, ctype, body = route_strategy(
            session, self.server.strategy_path, subpath, method, payload)
        self._send(code, body, ctype)


class _DashServer(ThreadingHTTPServer):
    """Serves the read-only dashboard and (if configured) the Strategy Advisor
    at /strategy. The strategy session is built lazily on first use so the
    dashboard works without OpenAI."""

    def __init__(self, addr, handler, db_path, config):
        super().__init__(addr, handler)
        self.db_path = db_path
        self.config = config
        self.strategy_path = getattr(config, "strategy_path", "") if config else ""
        self._session = None

    def strategy_session(self):
        if self._session is not None:
            return self._session, None
        if self.config is None:
            return None, "Strategy Advisor is not configured on this server."
        try:
            from .strategy_web import _build_session
            self._session = _build_session(self.config)
            return self._session, None
        except Exception as e:  # e.g. OPENAI_API_KEY missing
            return None, f"{type(e).__name__}: {e}"


def create_server(db_path: str, host: str = "127.0.0.1", port: int = 8765,
                  config=None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {"db_path": db_path})
    return _DashServer((host, port), handler, db_path, config)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765, config=None) -> None:
    srv = create_server(db_path, host, port, config)
    print(f"Dashboard (read-only) → http://{host}:{port}   (Ctrl-C to stop)")
    if config is not None:
        print(f"Strategy Advisor      → http://{host}:{port}/strategy")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
