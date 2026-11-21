#!/usr/bin/env python3
"""
web_panel.py  -  MIDx website dashboard + autopilot  (sirf Python stdlib, koi extra install nahi)

* MIDx terminal app (midx_terminal.py) ko ek pseudo-terminal mein chalata hai
* uska poora output website par live logs ki tarah dikhata hai
* AUTOPILOT: jo sawal terminal puchta hai uska jawab apne aap deta hai
      main menu  -> 3   (Start matches)
      accounts   -> all
      matches    -> 100
  (rules `autopilot.json` mein badal sakte ho - ya website ke box se haath se jawab bhejo)
* process band ho jaye to apne aap dobara chalata hai

Env:  PORT (default 8080) | MIDX_HOST (default 127.0.0.1, LAN ke liye 0.0.0.0)
      MIDX_MATCHES (default 100) | MIDX_AUTOPILOT (1/0) | MIDX_AUTO_RESTART (1/0)
      PANEL_KEY (set karoge to website/API par key lagegi)

NOTE: compiled .so modules sirf Termux (Android ARM) par chalte hain.
"""
import fcntl
import json
import os
import pty
import re
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("MIDX_HOST", "127.0.0.1")
MATCHES = os.environ.get("MIDX_MATCHES", "100")
PANEL_KEY = os.environ.get("PANEL_KEY", "")
AUTOPILOT_DEFAULT = os.environ.get("MIDX_AUTOPILOT", "1") != "0"
AUTO_RESTART = os.environ.get("MIDX_AUTO_RESTART", "1") != "0"
RESTART_DELAY = 15.0
IDLE_BEFORE_ANSWER = 0.8
MAX_LINES = 4000

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]")

# (name, scope, regex, answer)    scope: "prompt" = abhi ki adhuri line | "recent" = pichli lines
DEFAULT_RULES = [
    {"name": "press-enter", "scope": "prompt", "regex": r"press\s+enter|hit\s+enter|\[enter\]|enter\s+to\s+continue", "send": ""},
    {"name": "yes/no",      "scope": "prompt", "regex": r"\[y/n\]|\(y/n\)|\by/n\b|yes/no", "send": "y"},
    {"name": "matches",     "scope": "prompt", "regex": r"(how\s+many|number\s+of|total|count|enter|matches|rounds?).{0,40}(match|matches|games?|rounds?)", "send": "{MATCHES}"},
    {"name": "accounts",    "scope": "prompt", "regex": r"account|\(or\s+all\)|\ball\b|comma|range|squad", "send": "all"},
    {"name": "main-menu",   "scope": "prompt", "regex": r"(option|choice|menu|select|choose|enter).{0,30}(option|choice|number|menu|:)|^\s*>+\s*$|^\s*choice\s*[:>]", "send": "3"},
]


def load_rules():
    path = os.path.join(ROOT, "autopilot.json")
    if os.path.exists(path):
        try:
            data = json.load(open(path, "r", encoding="utf-8"))
            rules = []
            for r in data:
                re.compile(r["regex"])
                rules.append({"name": r.get("name", "rule"), "scope": r.get("scope", "prompt"),
                              "regex": r["regex"], "send": str(r.get("send", ""))})
            if rules:
                return rules
        except Exception as e:
            print(f"[PANEL] autopilot.json padhne mein error ({e}) - default rules use ho rahe hain")
    return DEFAULT_RULES


class Driver:
    def __init__(self):
        self.lock = threading.RLock()
        self.lines = deque(maxlen=MAX_LINES)
        self.base = 0                      # index of the first stored line
        self.partial = ""
        self.last_out = time.time()
        self.proc = None
        self.master = None
        self.started_at = None
        self.restarts = 0
        self.fast_fails = 0
        self.stopped_by_user = False
        self.autopilot = AUTOPILOT_DEFAULT
        self.awaiting_output = False
        self.sent_at = 0.0
        self.last_unmatched = ""
        self.answers = 0
        self.rules = load_rules()
        self.exit_code = None
        self.gave_up = False
        self.counters = {"rounds": 0, "matches_done": 0, "errors": 0}
        self.glory = deque(maxlen=6)

    # ───────────── logging ─────────────
    def _push(self, text, kind="out"):
        with self.lock:
            self.lines.append({"i": self.base + len(self.lines), "t": time.strftime("%H:%M:%S"),
                               "k": kind, "m": text})
            if len(self.lines) == MAX_LINES:
                pass
            self._count(text, kind)

    def note(self, text, kind="panel"):
        print(f"[PANEL] {text}", flush=True)
        self._push(text, kind)

    def _count(self, text, kind):
        if kind != "out":
            return
        low = text.lower()
        m = re.search(r"\bround\s+(\d+)", low)
        if m:
            self.counters["rounds"] = max(self.counters["rounds"], int(m.group(1)))
        if re.search(r"match.*(complete|finished|done|ended|over)", low) or re.search(r"(completed|finished).*match", low):
            self.counters["matches_done"] += 1
        if re.search(r"\b(error|failed|traceback|exception|banned)\b", low):
            self.counters["errors"] += 1
        if "glory" in low:
            self.glory.append(text.strip()[:200])

    # ───────────── child process ─────────────
    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            self.stopped_by_user = False
            self.exit_code = None
        cmd = os.environ.get("MIDX_CHILD_CMD")
        argv = cmd.split() if cmd else [sys.executable, "-u", os.path.join(ROOT, "midx_terminal.py")]
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 130, 0, 0))
        except Exception:
            pass
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("TERM", "xterm")
        try:
            proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=ROOT, env=env,
                                    preexec_fn=os.setsid, close_fds=True)
        except Exception as e:
            os.close(master)
            os.close(slave)
            self.note(f"❌ MIDx start nahi hua: {e}", "err")
            self.exit_code = -1
            return
        os.close(slave)
        with self.lock:
            self.master = master
            self.proc = proc
            self.started_at = time.time()
            self.partial = ""
            self.awaiting_output = False
            self.last_unmatched = ""
        self.note(f"▶ MIDx chalu (pid {proc.pid})")
        threading.Thread(target=self._reader, args=(master, proc), daemon=True).start()

    def _reader(self, master, proc):
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            self._feed(data.decode("utf-8", "replace"))
        try:
            rc = proc.wait(timeout=5)
        except Exception:
            rc = proc.poll()
        with self.lock:
            if self.partial.strip():
                self._push(self.partial, "out")
                self.partial = ""
            self.exit_code = rc
        up = time.time() - (self.started_at or time.time())
        self.note(f"⏹ MIDx band hua (exit code {rc}, {int(up)}s chala)", "err" if rc else "panel")
        try:
            os.close(master)
        except Exception:
            pass
        if self.stopped_by_user:
            return
        self.fast_fails = self.fast_fails + 1 if up < 10 else 0
        if not AUTO_RESTART:
            return
        if self.fast_fails >= 5:
            self.gave_up = True
            self.note("❌ 5 baar turant crash hua - restart band. Upar ke error logs dekho "
                      "(Termux ke bahar ye .so nahi chalte).", "err")
            return
        self.note(f"🔁 {int(RESTART_DELAY)}s baad dobara chalaunga...")
        time.sleep(RESTART_DELAY)
        if not self.stopped_by_user:
            self.restarts += 1
            self.start()

    def _feed(self, text):
        text = ANSI_RE.sub("", text)
        with self.lock:
            self.last_out = time.time()
            self.awaiting_output = False
            i = 0
            n = len(text)
            while i < n:
                ch = text[i]
                if ch == "\r":
                    if i + 1 < n and text[i + 1] == "\n":
                        i += 1
                        continue
                    self.partial = ""            # carriage return = line overwrite (progress bars)
                elif ch == "\n":
                    self._push(self.partial, "out")
                    self.partial = ""
                elif ch >= " " or ch == "\t":
                    self.partial += ch
                i += 1

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def send(self, text, source="manual"):
        if not self.alive() or self.master is None:
            return False
        with self.lock:                      # flag PEHLE set karo (agla sawal turant aa sakta hai)
            self.awaiting_output = True
            self.sent_at = time.time()
            self.last_out = time.time()
        try:
            os.write(self.master, (text + "\n").encode("utf-8"))
        except OSError:
            with self.lock:
                self.awaiting_output = False
            return False
        self.note(f"⌨ {source}: {text!r} bheja", "auto" if source == "auto" else "panel")
        return True

    def stop(self):
        self.stopped_by_user = True
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                pass
            self.note("⏹ MIDx ko stop bheja")

    def restart(self):
        self.stop()
        for _ in range(40):
            if not self.alive():
                break
            time.sleep(0.2)
        self.gave_up = False
        self.fast_fails = 0
        self.restarts += 1
        self.start()

    # ───────────── autopilot ─────────────
    def autopilot_loop(self):
        while True:
            time.sleep(0.25)
            try:
                self._autopilot_tick()
            except Exception as e:
                print(f"[PANEL] autopilot error: {e}", flush=True)

    def _autopilot_tick(self):
        if not self.autopilot or not self.alive():
            return
        if self.awaiting_output:
            if time.time() - self.sent_at > 4.0:      # safety: kuch nahi aaya to flag hata do
                self.awaiting_output = False
            else:
                return
        with self.lock:
            prompt = self.partial
            idle = time.time() - self.last_out
            recent = "\n".join(l["m"] for l in list(self.lines)[-25:])
        if not prompt.strip() or idle < IDLE_BEFORE_ANSWER:
            return
        for r in self.rules:
            hay = prompt if r["scope"] == "prompt" else recent + "\n" + prompt
            if re.search(r["regex"], hay, re.I | re.M):
                answer = r["send"].replace("{MATCHES}", str(MATCHES))
                self.answers += 1
                self.note(f"🤖 AUTO [{r['name']}]  sawal: {prompt.strip()[:80]!r}", "auto")
                self.send(answer, "auto")
                return
        if prompt != self.last_unmatched:
            self.last_unmatched = prompt
            self.note(f"⚠ sawal samajh nahi aaya: {prompt.strip()[:100]!r} - neeche box se jawab bhejo "
                      f"ya autopilot.json mein rule jodo", "err")

    # ───────────── data for the website ─────────────
    def snapshot(self, since):
        with self.lock:
            lines = [l for l in self.lines if l["i"] >= since]
            nxt = self.lines[-1]["i"] + 1 if self.lines else since
            return {
                "lines": lines[-800:], "next": nxt, "prompt": self.partial.strip(),
                "status": {
                    "running": self.alive(), "autopilot": self.autopilot, "restarts": self.restarts,
                    "uptime": int(time.time() - self.started_at) if self.started_at and self.alive() else 0,
                    "exit_code": self.exit_code, "gave_up": self.gave_up, "answers": self.answers,
                    "matches_target": MATCHES, "counters": dict(self.counters),
                    "glory": list(self.glory), "termux": bool(os.environ.get("PREFIX", "").startswith("/data/data/com.termux")),
                    "rules": [{"name": r["name"], "send": r["send"].replace("{MATCHES}", str(MATCHES))} for r in self.rules],
                },
            }


DRV = Driver()

PAGE = r"""<!DOCTYPE html>
<html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>MIDx · Dashboard</title>
<style>
:root{--bg:#0b0e17;--bg2:#121726;--card:#171d30;--line:#263050;--txt:#e8ecf8;--mut:#8b95b5;--acc:#6c8cff;--acc2:#a56bff;--ok:#27d98a;--warn:#ffb84d;--err:#ff5c7a}
@media(prefers-color-scheme:light){:root{--bg:#eef1f8;--bg2:#e3e8f4;--card:#fff;--line:#d3daec;--txt:#141a2e;--mut:#5d6788}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{min-height:100vh;background:radial-gradient(900px 400px at 10% -10%,rgba(108,140,255,.18),transparent 60%),radial-gradient(700px 400px at 100% 0,rgba(165,107,255,.15),transparent 55%),var(--bg)}
.wrap{max-width:1100px;margin:0 auto;padding:16px 14px 50px}
header{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
h1{margin:0;font-size:21px}h1 span{background:linear-gradient(90deg,var(--acc),var(--acc2));-webkit-background-clip:text;background-clip:text;color:transparent}
.pill{padding:5px 12px;border-radius:999px;border:1px solid var(--line);background:var(--card);font-size:12.5px;color:var(--mut)}.pill b{color:var(--txt)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--err);margin-right:6px}.dot.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}
.grid{display:grid;gap:10px}.stats{grid-template-columns:repeat(4,1fr);margin-bottom:10px}
@media(max-width:760px){.stats{grid-template-columns:repeat(2,1fr)}}
.k{color:var(--mut);font-size:12.5px}.v{font-size:23px;font-weight:800;margin-top:2px}
.warn{background:rgba(255,184,77,.12);border:1px solid rgba(255,184,77,.4);color:var(--warn);border-radius:12px;padding:10px 12px;margin-bottom:10px;font-size:13.5px;display:none}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
button{cursor:pointer;border:0;border-radius:12px;padding:11px 14px;font-weight:700;font-size:14.5px;color:#fff;background:linear-gradient(135deg,var(--acc),var(--acc2))}
button.g{background:var(--bg2);color:var(--txt);border:1px solid var(--line)}button.d{background:linear-gradient(135deg,#ff5c7a,#ff8a5c)}button.ok{background:linear-gradient(135deg,#1fcf82,#17a8a0)}
button:active{transform:scale(.98)}
.gl{margin-bottom:10px}.gl .l{font-size:13px;color:var(--ok);word-break:break-word}
.tabs{display:flex;gap:6px;margin:12px 0 6px;align-items:center;flex-wrap:wrap}.tabs button{padding:7px 12px;font-size:13px}.tabs .on{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;border:0}
.logs{background:#070a12;border:1px solid var(--line);border-radius:14px;padding:10px;height:420px;overflow:auto;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.logs .t{color:#5b6684;margin-right:8px}.logs div{white-space:pre-wrap;word-break:break-word;color:#cfd8f5}
.logs .auto{color:#58b7ff}.logs .panel{color:#a56bff}.logs .err{color:var(--err)}.logs .hl{color:var(--ok)}.logs .warnl{color:var(--warn)}
.send{display:flex;gap:8px;margin-top:8px}.send input{flex:1;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg2);color:var(--txt);font-size:16px;outline:none}
.pr{color:var(--warn);font:12.5px ui-monospace,Menlo,monospace;margin-top:6px;word-break:break-all}
small{color:var(--mut)}
</style></head><body><div class="wrap">
<header><h1>⚡ <span>MIDx</span> · Dashboard</h1><div class="pill"><span class="dot" id="dot"></span><b id="st">...</b></div></header>
<div class="warn" id="tw">⚠ Ye MIDx compiled modules sirf <b>Termux (Android ARM)</b> par chalte hain. Yahan (Render/PC) start error aayega - logs dekho.</div>
<div class="grid stats">
 <div class="card"><div class="k">Uptime</div><div class="v" id="up">00:00:00</div></div>
 <div class="card"><div class="k">Rounds dikhe</div><div class="v" id="rd">0</div></div>
 <div class="card"><div class="k">Matches done (log se)</div><div class="v" id="md">0</div></div>
 <div class="card"><div class="k">Restarts / Errors</div><div class="v" id="rs">0 / 0</div></div>
</div>
<div class="card gl"><div class="k">🏆 Glory / Guild lines (logs se) · Target: <b id="tg">100</b> matches</div><div id="gl" class="l"><small>Jab app "glory" likhega, yahan dikhega.</small></div></div>
<div class="row">
 <button class="ok" id="b3">▶ Start (3)</button><button id="bap" class="g">🤖 Autopilot: ?</button>
 <button class="g" id="brs">🔁 Restart</button><button class="d" id="bst">⏹ Stop</button>
</div>
<div class="tabs"><button class="on" data-f="all">Sab</button><button data-f="hl">Highlights</button><button data-f="err">Errors</button>
 <label style="margin-left:auto;font-size:13px;color:var(--mut)"><input type="checkbox" id="as" checked> auto-scroll</label></div>
<div class="logs" id="logs"></div><div class="pr" id="pr"></div>
<div class="send"><input id="inp" placeholder="terminal ko jawab bhejo (jaise 3, all, 100) aur Enter" autocomplete="off"><button id="bs">Send</button></div>
<small id="rules"></small>
</div>
<script>
const $=id=>document.getElementById(id);
let key=new URLSearchParams(location.search).get('key')||localStorage.getItem('k')||'';if(key)localStorage.setItem('k',key);
let next=0,all=[],filter='all',HL=/glory|guild|nick|name|joined|match|round|level|exp|squad/i;
const q=p=>p+(p.includes('?')?'&':'?')+'key='+encodeURIComponent(key);
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const fmt=s=>{s=Math.max(0,s|0);return String(s/3600|0).padStart(2,'0')+':'+String(s%3600/60|0).padStart(2,'0')+':'+String(s%60).padStart(2,'0')};
async function post(p,b){try{await fetch(q(p),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});}catch(e){}poll()}
$('b3').onclick=()=>post('/api/send',{text:'3'});$('bs').onclick=send;
$('inp').addEventListener('keydown',e=>{if(e.key==='Enter')send()});
function send(){const t=$('inp').value;post('/api/send',{text:t});$('inp').value=''}
$('brs').onclick=()=>post('/api/restart');$('bst').onclick=()=>{if(confirm('MIDx stop karna hai?'))post('/api/stop')};
$('bap').onclick=()=>post('/api/autopilot',{toggle:true});
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));b.classList.add('on');filter=b.dataset.f;draw()});
function cls(l){if(l.k==='auto')return'auto';if(l.k==='panel')return'panel';if(l.k==='err'||/\b(error|failed|traceback|exception|banned)\b/i.test(l.m))return'err';if(/glory|joined|success|✅/i.test(l.m))return'hl';if(/warn/i.test(l.m))return'warnl';return''}
function draw(){const L=all.filter(l=>filter==='all'||(filter==='hl'&&(HL.test(l.m)||l.k!=='out'))||(filter==='err'&&cls(l)==='err'));
 const box=$('logs');box.innerHTML=L.slice(-600).map(l=>`<div class="${cls(l)}"><span class="t">${l.t}</span>${esc(l.m)}</div>`).join('');if($('as').checked)box.scrollTop=box.scrollHeight}
async function poll(){try{const r=await fetch(q('/api/logs?since='+next),{cache:'no-store'});if(r.status===401){key=prompt('Panel key:')||'';localStorage.setItem('k',key);return}
 const d=await r.json();if(d.lines.length){all=all.concat(d.lines).slice(-2500);next=d.next;draw()}
 const s=d.status;$('dot').className='dot'+(s.running?' on':'');$('st').textContent=(s.running?'RUNNING':(s.gave_up?'CRASHED':'STOPPED'))+(s.exit_code!=null&&!s.running?' (exit '+s.exit_code+')':'');
 $('up').textContent=fmt(s.uptime);$('rd').textContent=s.counters.rounds;$('md').textContent=s.counters.matches_done;$('rs').textContent=s.restarts+' / '+s.counters.errors;$('tg').textContent=s.matches_target;
 $('bap').textContent='🤖 Autopilot: '+(s.autopilot?'ON':'OFF');$('tw').style.display=s.termux?'none':'block';
 $('gl').innerHTML=s.glory.length?s.glory.slice().reverse().map(g=>'<div>'+esc(g)+'</div>').join(''):'<small>Jab app "glory" likhega, yahan dikhega.</small>';
 $('pr').textContent=d.prompt?'⏳ terminal ka sawal: '+d.prompt:'';
 $('rules').textContent='Autopilot jawab: '+s.rules.map(r=>r.name+'→'+(r.send===''?'Enter':r.send)).join(' · ')+' | jawab: '+s.answers
}catch(e){}}
poll();setInterval(poll,1500);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MIDxPanel/1.0"

    def log_message(self, *a):
        pass

    def _auth(self, qs):
        if not PANEL_KEY:
            return True
        k = (qs.get("key") or [""])[0] or self.headers.get("X-Key", "")
        return k == PANEL_KEY

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/logs":
            if not self._auth(qs):
                return self._json({"error": "key"}, 401)
            try:
                since = int((qs.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            self._json(DRV.snapshot(since))
        elif u.path == "/health":
            self._json({"ok": True, "running": DRV.alive()})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._auth(qs):
            return self._json({"error": "key"}, 401)
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            body = {}
        if u.path == "/api/send":
            ok = DRV.send(str(body.get("text", ""))[:200], "manual")
            self._json({"ok": ok})
        elif u.path == "/api/restart":
            threading.Thread(target=DRV.restart, daemon=True).start()
            self._json({"ok": True})
        elif u.path == "/api/stop":
            DRV.stop()
            self._json({"ok": True})
        elif u.path == "/api/autopilot":
            DRV.autopilot = (not DRV.autopilot) if body.get("toggle") else bool(body.get("on", True))
            DRV.note(f"🤖 Autopilot {'ON' if DRV.autopilot else 'OFF'}")
            self._json({"ok": True, "autopilot": DRV.autopilot})
        else:
            self._json({"error": "not found"}, 404)


def main():
    print("=" * 56)
    print("  MIDx Dashboard")
    print(f"  Browser mein kholo:  http://127.0.0.1:{PORT}   (host {HOST})")
    print(f"  Autopilot: {'ON' if AUTOPILOT_DEFAULT else 'OFF'} | menu 3 | matches {MATCHES}")
    if PANEL_KEY:
        print("  PANEL_KEY laga hai (URL ke aage ?key=... lagao)")
    if not os.environ.get("PREFIX", "").startswith("/data/data/com.termux"):
        print("  ⚠ Ye Termux nahi lag raha - compiled .so modules yahan nahi chalenge.")
    print("=" * 56, flush=True)
    DRV.start()
    threading.Thread(target=DRV.autopilot_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        DRV.stop()
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
