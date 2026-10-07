"""Local browser console for cached Codex agents."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

from codex_tools import paths, relay_cache
from codex_tools.browser import open_browser


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_RECORD_LIMIT = 240
STATE_FILE = paths.AGENTS_UI_STATE_DIR / "console.json"
LOG_FILE = paths.AGENTS_UI_STATE_DIR / "console.log"


CONSOLE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Console</title><style>
:root{color-scheme:dark;--bg:#101113;--panel:#17191d;--line:#2a2d33;--muted:#969da8;--text:#e8eaed;--accent:#75a99c;--user:#24342f;--assistant:#1c2025}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif;height:100vh;overflow:hidden}
button,select,textarea,input{font:inherit;color:inherit;background:#202329;border:1px solid var(--line);border-radius:7px}button{padding:.5rem .75rem;cursor:pointer}button:hover{border-color:#59616d}.app{display:grid;grid-template-columns:310px 1fr;height:100vh}
aside{background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;min-width:0}.brand{padding:18px 16px 12px;font-size:18px;font-weight:650}.controls{padding:0 12px 12px;display:grid;gap:8px}.controls select,.controls input{width:100%;padding:.55rem}.agents{overflow:auto;border-top:1px solid var(--line)}
.agent{display:block;width:100%;text-align:left;border:0;border-bottom:1px solid #22252a;border-radius:0;background:transparent;padding:12px}.agent.active{background:#24282e;border-left:3px solid var(--accent);padding-left:9px}.agent-title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}.meta{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px}
main{display:grid;grid-template-rows:auto 1fr auto;min-width:0}.header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}.header-info{min-width:0;flex:1}.header h1{font-size:16px;margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.conversation{overflow:auto;padding:24px max(24px,calc((100% - 860px)/2));scrollbar-gutter:stable}.bubble{padding:14px 16px;border:1px solid var(--line);border-radius:12px;margin:0 0 14px;white-space:pre-wrap;overflow-wrap:anywhere}.bubble.user{background:var(--user);margin-left:12%}.bubble.assistant{background:var(--assistant);margin-right:8%}.role{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}.empty{color:var(--muted);text-align:center;margin-top:20vh}
.composer{border-top:1px solid var(--line);padding:12px max(20px,calc((100% - 860px)/2));display:flex;gap:10px;background:var(--panel)}textarea{flex:1;resize:none;min-height:44px;max-height:140px;padding:10px 12px}.send{align-self:flex-end;background:#31584f}.status{position:fixed;right:16px;bottom:76px;padding:8px 12px;border-radius:7px;background:#292d34;box-shadow:0 3px 18px #0008;display:none}.status.show{display:block}.kbd{font-size:11px;color:var(--muted)}
@media(max-width:720px){.app{grid-template-columns:130px 1fr}.brand{font-size:15px}.controls{padding:0 7px 8px}.agent{padding:9px 7px}.agent.active{padding-left:4px}.conversation{padding:14px}.header{padding:10px}.kbd{display:none}}
</style></head><body><div class="app"><aside><div class="brand">Agent Console</div><div class="controls"><select id="host"></select><input id="filter" placeholder="Find agent…" aria-label="Find agent"></div><div class="agents" id="agents"></div></aside><main><div class="header"><div class="header-info"><h1 id="title">Choose an agent</h1><div class="meta" id="details"></div></div><span class="kbd">Ctrl P · Alt J/K · M · V · S</span><button id="refresh">Refresh</button><button id="viewer">Viewer</button></div><div class="conversation" id="conversation"><div class="empty">Choose an agent from the left.</div></div><form class="composer" id="composer"><textarea id="message" placeholder="Message this agent…" disabled></textarea><button class="send" id="send" disabled>Send</button></form></main></div><div id="status" class="status"></div>
<script>
const $=s=>document.querySelector(s), state={host:'local',agents:[],selected:null};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(text){const e=$('#status');e.textContent=text;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),2600)}
function age(value){if(!value)return 'unknown';const n=new Date(value),d=(Date.now()-n)/1000;if(d<60)return 'now';if(d<3600)return Math.floor(d/60)+'m';if(d<86400)return Math.floor(d/3600)+'h';return Math.floor(d/86400)+'d'}
function renderAgents(){const q=$('#filter').value.toLowerCase();const list=state.agents.filter(a=>(a.title+' '+a.cwd+' '+a.agent_id).toLowerCase().includes(q));$('#agents').innerHTML=list.map(a=>`<button class="agent ${state.selected===a.agent_id?'active':''}" data-id="${esc(a.agent_id)}"><div class="agent-title">${esc(a.title||a.agent_id)}</div><div class="meta">${esc(age(a.updated_at))} · ${esc(a.cwd||'no basedir')}</div></button>`).join('')||'<div class="empty">No agents</div>';document.querySelectorAll('.agent').forEach(e=>e.onclick=()=>select(e.dataset.id))}
async function load(agent=state.selected){const p=new URLSearchParams({host:state.host});if(agent)p.set('agent',agent);const r=await fetch('/api/state?'+p);const data=await r.json();if(!r.ok)throw Error(data.error||r.statusText);$('#host').innerHTML=data.hosts.map(h=>`<option value="${esc(h.host_id)}" ${h.host_id===state.host?'selected':''}>${esc(h.name)} (${h.agent_count})</option>`).join('');state.agents=data.agents;state.selected=data.selected?.agent_id||null;renderAgents();renderConversation(data.selected,data.records,data.records_truncated);}
function renderConversation(agent,records,truncated){$('#title').textContent=agent?.title||'Choose an agent';$('#details').textContent=agent?`${agent.cwd||'No basedir'} · ${agent.agent_id}`:'';$('#message').disabled=!agent;$('#send').disabled=!agent;$('#viewer').disabled=!agent;if(!agent){$('#conversation').innerHTML='<div class="empty">Choose an agent from the left.</div>';return}const notice=truncated?'<div class="meta" style="text-align:center;margin-bottom:12px">Showing recent messages</div>':'';const messages=records.filter(r=>r.role==='user'||r.role==='assistant');$('#conversation').innerHTML=messages.length?notice+messages.map(r=>`<div class="bubble ${esc(r.role)}"><div class="role">${esc(r.role)}</div>${esc(r.text)}</div>`).join(''):'<div class="empty">No renderable messages.</div>';$('#conversation').scrollTop=$('#conversation').scrollHeight}
async function select(id){state.selected=id;history.replaceState(null,'','#'+encodeURIComponent(state.host+':'+id));await load(id)}
async function post(path,payload={}){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();if(!r.ok)throw Error(data.error||r.statusText);return data}
$('#host').onchange=async e=>{state.host=e.target.value;state.selected=null;await load()};$('#filter').oninput=renderAgents;$('#refresh').onclick=async()=>{try{toast('Refreshing…');const d=await post('/api/refresh');await load();toast(`Refreshed ${d.count} local agents`)}catch(e){toast(e.message)}};
$('#composer').onsubmit=async e=>{e.preventDefault();const text=$('#message').value.trim();if(!text||!state.selected)return;$('#send').disabled=true;try{const d=await post('/api/send',{host:state.host,agent:state.selected,message:text});$('#message').value='';toast(d.delivery==='sent'?'Message sent':'Message queued for sync')}catch(e){toast(e.message)}finally{$('#send').disabled=false}};
$('#message').onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();$('#composer').requestSubmit()}};$('#viewer').onclick=()=>{if(state.selected)window.open('http://127.0.0.1:8765/v/'+encodeURIComponent(state.selected),'_blank')};
document.addEventListener('keydown',e=>{if(e.ctrlKey&&e.key.toLowerCase()==='p'){e.preventDefault();$('#filter').focus()}if(!e.ctrlKey&&!e.metaKey&&e.key.toLowerCase()==='m'&&!['INPUT','TEXTAREA'].includes(document.activeElement.tagName)){$('#message').focus()}if(!e.ctrlKey&&!e.metaKey&&e.key.toLowerCase()==='s'&&!['INPUT','TEXTAREA'].includes(document.activeElement.tagName)){$('#refresh').click()}if(!e.ctrlKey&&!e.metaKey&&e.key.toLowerCase()==='v'&&!['INPUT','TEXTAREA'].includes(document.activeElement.tagName)){$('#viewer').click()}if(e.altKey&&['j','k'].includes(e.key.toLowerCase())){e.preventDefault();const i=Math.max(0,state.agents.findIndex(a=>a.agent_id===state.selected));const n=e.key.toLowerCase()==='j'?Math.min(state.agents.length-1,i+1):Math.max(0,i-1);if(state.agents[n])select(state.agents[n].agent_id)}});
const hash=decodeURIComponent(location.hash.slice(1));if(hash.includes(':')){const [h,...id]=hash.split(':');state.host=h;state.selected=id.join(':')}load().catch(e=>toast(e.message));
</script></body></html>"""


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def state_payload(cache: relay_cache.RelayCache, query: dict[str, list[str]]) -> dict[str, Any]:
    hosts = cache.hosts()
    host_id = (query.get("host") or [hosts[0]["host_id"] if hosts else "local"])[0]
    cached_agents = cache.agents(host_id)
    requested = (query.get("agent") or [""])[0]
    selected = next((item for item in cached_agents if item["agent_id"] == requested), None)
    if selected is None and cached_agents:
        selected = cached_agents[0]
    all_records = cache.records(host_id, selected["agent_id"]) if selected else []
    records = all_records[-DEFAULT_RECORD_LIMIT:]
    return {
        "hosts": hosts,
        "host": host_id,
        "agents": cached_agents,
        "selected": selected,
        "records": records,
        "record_count": len(all_records),
        "records_truncated": len(records) < len(all_records),
    }


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    if handler.headers.get_content_type() != "application/json":
        raise ValueError("Content-Type must be application/json")
    length = int(handler.headers.get("Content-Length", "0"))
    if length < 1 or length > 128_000:
        raise ValueError("invalid request size")
    payload = json.loads(handler.rfile.read(length))
    if not isinstance(payload, dict):
        raise ValueError("JSON object required")
    return payload


def send_message(cache: relay_cache.RelayCache, payload: dict[str, Any], codex_bin: str) -> dict[str, str]:
    host_id = str(payload.get("host") or "")
    agent_id = str(payload.get("agent") or "")
    message = str(payload.get("message") or "").strip()
    if not message:
        raise ValueError("a non-empty message is required")
    agent = next((item for item in cache.agents(host_id) if item["agent_id"] == agent_id), None)
    if agent is None:
        raise FileNotFoundError("cached agent not found")
    if host_id != "local":
        message_id = cache.enqueue(host_id, agent_id, {"text": message})
        return {"delivery": "queued", "message_id": message_id}
    thread_id = str(agent.get("thread_id") or agent.get("session_id") or agent_id)
    completed = subprocess.run(
        [codex_bin, "queue", "--thread", thread_id, "--message", message],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit status {completed.returncode}"
        raise RuntimeError(f"codex queue failed: {detail}")
    return {"delivery": "sent", "message_id": ""}


def make_handler(cache: relay_cache.RelayCache, sessions_root: Path, session_index: Path, codex_bin: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    body = CONSOLE_HTML.encode()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers(); self.wfile.write(body)
                elif parsed.path == "/api/state":
                    json_response(self, state_payload(cache, parse_qs(parsed.query)))
                elif parsed.path == "/api/health":
                    json_response(self, {"service": "codex-tools-agent-console", "schema": relay_cache.SCHEMA_VERSION})
                else:
                    json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (OSError, ValueError, sqlite3.Error) as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:
            try:
                payload = read_json(self)
                if self.path == "/api/refresh":
                    count = relay_cache.refresh_local(cache, sessions_root, session_index)
                    json_response(self, {"count": count})
                elif self.path == "/api/send":
                    json_response(self, send_message(cache, payload, codex_bin))
                else:
                    json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (FileNotFoundError, ValueError) as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
    return Handler


def console_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def is_loopback(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def serve(args: argparse.Namespace) -> int:
    if not is_loopback(args.host):
        print(
            "error: Agent Console is unauthenticated and may only bind to loopback",
            file=sys.stderr,
        )
        return 2
    cache = relay_cache.RelayCache(args.cache_file)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(cache, args.sessions_root, args.session_index, args.codex_bin))
    print(f"Agent Console: {console_url(args.host, args.port)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def read_state() -> dict[str, Any] | None:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        pid = int(payload.get("pid", 0))
        os.kill(pid, 0)
        return payload
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        try: STATE_FILE.unlink()
        except OSError: pass
        return None


def healthy(url: str) -> bool:
    try:
        with urlopen(url.rstrip("/") + "/api/health", timeout=.6) as response:
            return response.status == HTTPStatus.OK and json.load(response).get("service") == "codex-tools-agent-console"
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def stop_console() -> int:
    state = read_state()
    if not state:
        print("Agent Console is not running.")
        return 0
    pid = int(state["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5
    while time.time() < deadline:
        try: os.kill(pid, 0)
        except ProcessLookupError: break
        time.sleep(.1)
    try: STATE_FILE.unlink()
    except OSError: pass
    print("Stopped Agent Console.")
    return 0


def command_ui(args: argparse.Namespace) -> int:
    if args.stop:
        return stop_console()
    current = read_state()
    if args.status:
        if current and healthy(str(current["url"])):
            print(f"Agent Console running: {current['url']}")
            print(f"PID: {current['pid']}")
            print(f"Log: {current['log']}")
            return 0
        print("Agent Console is not running.")
        return 1
    if not is_loopback(args.host):
        print(
            "error: Agent Console is unauthenticated and may only bind to loopback",
            file=sys.stderr,
        )
        return 2
    cache = relay_cache.RelayCache(args.cache_file)
    if not args.no_refresh:
        count = relay_cache.refresh_local(cache, args.sessions_root, args.session_index)
        print(f"Refreshed {count} local agent session(s).")
    if current:
        url = str(current["url"])
        if not healthy(url):
            print(f"error: Agent Console process {current['pid']} is not responding; run `ct agents ui --stop`", file=sys.stderr)
            return 1
    else:
        paths.ensure_private_dir(paths.AGENTS_UI_STATE_DIR)
        log = LOG_FILE.open("a", encoding="utf-8"); LOG_FILE.chmod(0o600)
        command = [sys.executable, "-m", "codex_tools.agents_ui", "--serve", "--host", args.host, "--port", str(args.port), "--cache-file", str(args.cache_file), "--sessions-root", str(args.sessions_root), "--session-index", str(args.session_index), "--codex-bin", args.codex_bin]
        try:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            log.close()
        url = console_url(args.host, args.port)
        paths.write_private_text(STATE_FILE, json.dumps({"pid": process.pid, "url": url, "log": str(LOG_FILE), "started_at": datetime.now().isoformat()}, indent=2) + "\n")
        deadline = time.time() + 5
        while time.time() < deadline and process.poll() is None and not healthy(url):
            time.sleep(.1)
        if process.poll() is not None or not healthy(url):
            try: STATE_FILE.unlink()
            except OSError: pass
            print(f"error: Agent Console failed to start; see {LOG_FILE}", file=sys.stderr)
            return 1
        print(f"Started Agent Console: {url}")
    if not args.no_open and not open_browser(url, args):
        print(f"Agent Console remains available at {url}", file=sys.stderr)
        return 1
    elif args.no_open:
        print(f"Agent Console: {url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--cache-file", type=Path, required=True)
    parser.add_argument("--sessions-root", type=Path, required=True)
    parser.add_argument("--session-index", type=Path, required=True)
    parser.add_argument("--codex-bin", default="codex")
    return serve(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
