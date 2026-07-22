"""Interactive Strategy Advisor web UI (chat + document upload -> criteria YAML).

A small local web app: chat with the advisor, upload documents (résumé, job
descriptions, brag docs), watch the machine-usable criteria YAML update live, and
Save it to strategy.md. Backed by OpenAI via ``StrategySession``.

Localhost-only. Writes ONLY strategy.md on explicit Save (SAFE tier, local output).
It cannot crawl, screen, or submit — this is purely the strategy authoring surface.
"""

from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agents.doc_ingest import DocIngestError, extract_text
from .agents.strategy_session import StrategySession

_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Strategy Advisor</title>
<style>
 :root{color-scheme:light dark;--bg:#0f1420;--card:#1a2130;--fg:#e7ecf3;--muted:#93a1b5;
   --line:#2a3446;--accent:#5b9dff;--you:#26324a;--bot:#1e2a1f}
 @media(prefers-color-scheme:light){:root{--bg:#f4f6fb;--card:#fff;--fg:#101828;
   --muted:#5b667a;--line:#e4e8f0;--accent:#2f6fed;--you:#e7efff;--bot:#eaf5ec}}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
   font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;height:100vh;overflow:hidden}
 header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center}
 h1{font-size:15px;margin:0;font-weight:650}.muted{color:var(--muted);font-size:12px}
 .wrap{display:grid;grid-template-columns:1fr 420px;height:calc(100vh - 49px)}
 .col{display:flex;flex-direction:column;min-height:0}.col.right{border-left:1px solid var(--line)}
 .msgs{flex:1;overflow:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
 .msg{padding:9px 12px;border-radius:12px;max-width:80%;white-space:pre-wrap}
 .msg.user{align-self:flex-end;background:var(--you)}.msg.assistant{align-self:flex-start;background:var(--bot)}
 .bar{border-top:1px solid var(--line);padding:10px;display:flex;gap:8px;align-items:flex-end}
 textarea{flex:1;resize:none;background:var(--card);color:var(--fg);border:1px solid var(--line);
   border-radius:10px;padding:9px 11px;font:inherit;min-height:42px;max-height:140px}
 button{background:var(--accent);color:#fff;border:0;border-radius:10px;padding:10px 14px;
   font-weight:600;cursor:pointer}button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
 .right .pad{padding:14px;overflow:auto;flex:1}
 h2{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 8px}
 pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;
   font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre;margin:0}
 .chip{display:inline-block;background:var(--card);border:1px solid var(--line);border-radius:20px;
   padding:2px 9px;margin:2px 4px 2px 0;font-size:12px}
 .warn{color:#ffb454;font-size:12px;margin-top:8px}.ok{color:#3ddc84;font-size:12px}
 label.up{display:inline-block;border:1px dashed var(--line);border-radius:10px;padding:8px 12px;
   cursor:pointer;color:var(--muted);font-size:13px}
</style></head><body>
<header><a id="nav" href="/" class="ghost" style="display:none;text-decoration:none;padding:6px 10px;border-radius:8px;border:1px solid var(--line);color:var(--fg)">← Dashboard</a>
  <h1>Strategy Advisor</h1><span class="muted" id="sub">chat + documents → screening criteria</span></header>
<div class="wrap">
 <div class="col">
   <div class="msgs" id="msgs"></div>
   <div class="bar">
     <label class="up">📎 doc<input id="file" type="file" style="display:none"
        accept=".txt,.md,.pdf,.docx,.csv,.json"></label>
     <textarea id="in" placeholder="Tell the advisor about your goals, or ask a question…"></textarea>
     <button id="send">Send</button>
   </div>
 </div>
 <div class="col right">
   <div class="pad">
     <h2>Documents</h2><div id="docs" class="muted">none yet</div>
     <button id="draftbank" class="ghost" style="margin-top:8px;font-size:12px;padding:6px 10px">✨ Draft bank entries from docs</button>
     <div id="drafts"></div>
     <h2 style="margin-top:16px">Screening criteria (YAML)</h2>
     <pre id="yaml">—</pre>
     <div id="warn" class="warn"></div>
   </div>
   <div class="bar" style="justify-content:flex-end">
     <span id="saved" class="ok"></span>
     <button id="save" class="ghost">Save to strategy.md</button>
   </div>
 </div>
</div>
<script>
const $=s=>document.querySelector(s);
// Works whether served standalone at "/" or mounted under "/strategy".
const BASE=(location.pathname.replace(/\/+$/,"")==="/strategy")?"/strategy":"";
if(BASE){document.getElementById("nav").style.display="inline";}
function render(st){
  $("#msgs").innerHTML = (st.messages||[]).map(m=>`<div class="msg ${m.role}">${esc(m.content)}</div>`).join("");
  $("#msgs").scrollTop = 1e9;
  $("#yaml").textContent = st.yaml || "—";
  $("#docs").innerHTML = (st.documents&&st.documents.length)
     ? st.documents.map(d=>`<span class="chip">${esc(d)}</span>`).join("") : '<span class="muted">none yet</span>';
  const u = st.ungrounded_must_haves||[];
  $("#warn").textContent = u.length ? ("⚠ must-haves not grounded in your bank/aspirations: "+u.join(", ")) : "";
  const d = st.draft_entries||[];
  if(d.length){
    $("#drafts").innerHTML = '<div class="muted" style="margin:8px 0 4px">Proposed bank entries (review, then add):</div>'
      + d.map(e=>`<div class="chip" style="display:block;margin:4px 0;padding:8px 10px;white-space:normal">
          <b>${esc(e.employer)} — ${esc(e.title)}</b> <span class="muted">${esc(e.start_date)}–${esc(e.end_date)}</span><br>
          ${esc(e.text)}<br><span class="muted">metrics: ${esc((e.metrics||[]).join(", "))} · skills: ${esc((e.skills||[]).join(", "))}</span>
        </div>`).join("")
      + `<button id="addbank">Add ${d.length} to accomplishment_bank.yaml</button>`;
    $("#addbank").onclick=async()=>{const r=await post("/api/commit_bank",{entries:d});
      $("#drafts").innerHTML='<div class="ok">Added '+(r.added||0)+' entries to your bank.</div>';};
  } else if($("#drafts").dataset.keep!=="1"){ $("#drafts").innerHTML=""; }
}
const esc=s=>String(s==null?"":s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function post(url,body){const r=await fetch(BASE+url,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(body||{})});return r.json();}
async function load(){render(await (await fetch(BASE+"/api/state")).json());}
async function send(){const t=$("#in").value.trim();if(!t)return;$("#in").value="";
  $("#send").disabled=true;$("#send").textContent="…";
  render(await post("/api/message",{text:t}));$("#send").disabled=false;$("#send").textContent="Send";}
$("#send").onclick=send;
$("#in").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();}});
$("#file").onchange=async e=>{const f=e.target.files[0];if(!f)return;
  const b64=await new Promise(res=>{const r=new FileReader();r.onload=()=>res(r.result.split(",")[1]);r.readAsDataURL(f);});
  $("#sub").textContent="reading "+f.name+"…";
  const st=await post("/api/upload",{name:f.name,b64});
  $("#sub").textContent = st.error? ("upload error: "+st.error) : "uploaded "+f.name;
  render(st); e.target.value="";};
$("#save").onclick=async()=>{const r=await post("/api/save",{});$("#saved").textContent="saved → "+r.saved;
  setTimeout(()=>$("#saved").textContent="",4000);};
$("#draftbank").onclick=async()=>{$("#drafts").innerHTML='<span class="muted">drafting…</span>';
  render(await post("/api/draft_bank",{}));};
load();
</script></body></html>"""


def strategy_html() -> bytes:
    return _HTML.encode("utf-8")


def route_strategy(session: StrategySession, strategy_path: str, subpath: str,
                   method: str, payload: dict) -> tuple[int, str, bytes]:
    """Route one strategy request. Reused by the standalone server and the unified
    dashboard (mounted under /strategy). Returns (status, content_type, body)."""
    def j(obj):
        return (200, "application/json", json.dumps(obj).encode("utf-8"))
    sp = subpath or "/"
    if method == "GET":
        if sp in ("", "/", "/index.html"):
            return (200, "text/html; charset=utf-8", strategy_html())
        if sp.startswith("/api/state"):
            return j(session.state())
        return (404, "text/plain", b"not found")
    if method == "POST":
        if sp == "/api/message":
            text = str(payload.get("text", "")).strip()
            return j(session.send(text) if text else session.state())
        if sp == "/api/upload":
            name = payload.get("name", "document")
            try:
                text = extract_text(name, base64.b64decode(payload.get("b64", "")))
            except DocIngestError as e:
                return j({**session.state(), "error": str(e)})
            except Exception as e:
                return j({**session.state(), "error": f"decode failed: {e}"})
            session.add_document(name, text)
            return j(session.note_document(name))
        if sp == "/api/save":
            return j({"saved": session.save(strategy_path)})
        if sp == "/api/draft_bank":
            return j({**session.state(), "draft_entries": session.draft_bank_entries()})
        if sp == "/api/commit_bank":
            entries = payload.get("entries") or session.draft_entries
            return j({**session.state(), "added": session.commit_bank_entries(entries)})
    return (404, "text/plain", b"not found")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _dispatch(self, method: str):
        payload = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n:
                try:
                    payload = json.loads(self.rfile.read(n).decode("utf-8"))
                except json.JSONDecodeError:
                    payload = {}
        code, ctype, body = route_strategy(
            self.server.session, self.server.strategy_path, self.path, method, payload)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


class _Server(ThreadingHTTPServer):
    def __init__(self, addr, session: StrategySession, strategy_path: str):
        super().__init__(addr, _Handler)
        self.session = session
        self.strategy_path = strategy_path


def create_server(session: StrategySession, strategy_path: str,
                  host: str = "127.0.0.1", port: int = 8766) -> _Server:
    return _Server((host, port), session, strategy_path)


def _build_session(config):
    """Build a StrategySession from config (OpenAI + bank + profile + bank_path)."""
    from .llm.openai_llm import OpenAILLM
    raw = config.raw or {}
    lc = raw.get("llm", {}) or {}
    llm = OpenAILLM(model=lc.get("model", "gpt-4o-mini"),
                    reasoning_effort=lc.get("reasoning_effort"),
                    max_tokens=int(lc.get("max_tokens", 2000)))
    try:
        import yaml
        from pathlib import Path
        from .models import AccomplishmentBank
        bank = AccomplishmentBank.from_dict(
            yaml.safe_load(Path(config.accomplishment_bank_path).read_text("utf-8")) or {})
    except Exception:
        bank = None
    return StrategySession(llm, profile=raw.get("profile", {}), bank=bank,
                           bank_path=config.accomplishment_bank_path)


def serve(config, *, host: str = "127.0.0.1", port: int = 8766) -> None:
    """Build a session from config (OpenAI + bank + profile) and serve the UI."""
    session = _build_session(config)   # chat requires OpenAI; needs OPENAI_API_KEY
    srv = create_server(session, config.strategy_path, host, port)
    print(f"Strategy Advisor (chat) → http://{host}:{port}   (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
