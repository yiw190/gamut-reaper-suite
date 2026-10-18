#!/usr/bin/env python3
"""Agent gateway: one local REST + SSE API for the REAPER chat panel.

Gamut agents are proxied to the Gamut desktop API unchanged. ACP agents (Codex, Claude Code, Gemini CLI, ...)
are run as stdio subprocesses and exposed under the same routes as Gamut, as agents with slug `acp-<slug>`:

  GET  /api/agents                                   Gamut agents + ACP agents
  GET  /api/agents/<slug>/config                     model / effort / mode selectors (normalized config options)
  GET  /api/agents/<slug>/status                     ACP: {state, authMethods, error}
  POST /api/agents/<slug>/auth          {methodId}   ACP: start login
  GET  /api/agents/<slug>/sessions
  POST /api/agents/<slug>/sessions      {message, config}
  POST /api/agents/<slug>/sessions/<id>/messages {content, config}
  GET  /api/agents/<slug>/sessions/<id>/messages
  GET  /api/agents/<slug>/sessions/<id>/stream       SSE: connected, stream_delta, tool_use_ready, session_idle,
                                                     session_error, permission_request, permission_resolved,
                                                     config_options
  POST /api/agents/<slug>/sessions/<id>/interrupt
  POST /api/agents/<slug>/sessions/<id>/permission {requestId, optionId|null}

Stdlib only; runs on macOS' /usr/bin/python3 (3.9). Listens on 127.0.0.1 only.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("AGENT_GATEWAY_PORT", "47892"))
GAMUT = os.environ.get("GAMUT_URL", "http://127.0.0.1:47891").rstrip("/")
CONFIG = Path(os.environ.get("AGENT_GATEWAY_CONFIG", "~/.config/agent-gateway/config.json")).expanduser()
STATE = Path(os.environ.get("AGENT_GATEWAY_STATE", str(CONFIG.parent / "sessions"))).expanduser()
ACP_PREFIX = "acp-"
AUTH_REQUIRED = -32000


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def expand(v):
    return os.path.expandvars(os.path.expanduser(v)) if isinstance(v, str) else v


def load_config():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())
    return {"acp": [], "mcpServers": []}


class AcpError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


# ---------------------------------------------------------------- sessions

class Session:
    def __init__(self, agent, sid=None, title="", created=None):
        self.agent = agent
        self.id = sid or str(uuid.uuid4())
        self.acp_id = None
        self.title = title
        self.created = created or now_iso()
        self.updated = self.created
        self.msgs = []
        self.active = False
        self.subs = set()
        self.perms = {}  # requestId -> (rpc id, params)
        self.config = []
        self.applied = {}
        self.backlog = []
        self.loading = False
        self.lock = threading.RLock()

    def emit(self, kind, **data):
        data["type"] = kind
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            q.put(data)

    def public(self):
        return {"id": self.id, "name": self.title or "New chat", "isActive": self.active,
                "lastActivityAt": self.updated, "createdAt": self.created}

    def assistant(self):
        if not self.msgs or self.msgs[-1]["type"] != "assistant":
            self.msgs.append({"id": str(uuid.uuid4()), "type": "assistant", "content": {"text": ""},
                              "toolCalls": [], "createdAt": now_iso()})
        return self.msgs[-1]

    def path(self):
        return STATE / self.agent.slug / (self.id + ".json")

    def persist(self):
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": self.id, "acp_id": self.acp_id, "title": self.title,
                                   "created": self.created, "updated": self.updated, "msgs": self.msgs}))
        tmp.replace(p)

    # ACP session/update
    def on_update(self, u):
        kind = u.get("sessionUpdate")
        if self.loading:
            return
        if kind == "agent_message_chunk":
            c = u.get("content") or {}
            if c.get("type") == "text" and c.get("text"):
                with self.lock:
                    m = self.assistant()
                    m["content"]["text"] += c["text"]
                self.emit("stream_delta", text=c["text"])
        elif kind == "tool_call":
            title = u.get("title") or u.get("name") or u.get("kind") or "tool"
            with self.lock:
                self.assistant()["toolCalls"].append({"id": u.get("toolCallId"), "name": title})
            self.emit("tool_use_ready", toolName=title)
        elif kind == "tool_call_update" and u.get("title"):
            with self.lock:
                for tc in self.assistant()["toolCalls"]:
                    if tc.get("id") == u.get("toolCallId"):
                        tc["name"] = u["title"]
        elif kind == "config_option_update" and isinstance(u.get("configOptions"), list):
            self.config = u["configOptions"]
            self.agent.config_cache = self.config
            self.emit("config_options", configOptions=self.config)
        elif kind == "current_mode_update":
            for o in self.config:
                if o.get("category") == "mode":
                    o["currentValue"] = u.get("currentModeId")
            self.emit("config_options", configOptions=self.config)


def normalize_config(result):
    opts = result.get("configOptions")
    if isinstance(opts, list):
        return opts
    out = []
    models = result.get("models")
    if isinstance(models, dict) and models.get("availableModels"):
        out.append({"id": "model", "name": "Model", "category": "model", "type": "select", "_legacy": "model",
                    "currentValue": models.get("currentModelId"),
                    "options": [{"value": m.get("modelId"), "name": m.get("name") or m.get("modelId"),
                                 "description": m.get("description", "")} for m in models["availableModels"]]})
    modes = result.get("modes")
    if isinstance(modes, dict) and modes.get("availableModes"):
        out.append({"id": "mode", "name": "Mode", "category": "mode", "type": "select", "_legacy": "mode",
                    "currentValue": modes.get("currentModeId"),
                    "options": [{"value": m.get("id"), "name": m.get("name") or m.get("id"),
                                 "description": m.get("description", "")} for m in modes["availableModes"]]})
    return out


# ---------------------------------------------------------------- ACP agent process

class AcpAgent:
    def __init__(self, spec, mcp_servers):
        self.spec = spec
        self.slug = ACP_PREFIX + spec["slug"]
        self.name = spec.get("name", spec["slug"])
        self.mcp_servers = mcp_servers
        self.proc = None
        self.lock = threading.RLock()
        self.wlock = threading.Lock()
        self.next_id = 0
        self.pending = {}
        self.state = "stopped"
        self.error = None
        self.connected = False
        self.probing = False
        self.auth_methods = []
        self.caps = {}
        self.sessions = {}
        self.by_acp = {}
        self.config_cache = []
        self.warm = None
        self.load_persisted()

    # -- persistence
    def load_persisted(self):
        d = STATE / self.slug
        if not d.is_dir():
            return
        for p in d.glob("*.json"):
            try:
                j = json.loads(p.read_text())
            except ValueError:
                continue
            s = Session(self, j["id"], j.get("title", ""), j.get("created"))
            s.updated = j.get("updated", s.created)
            s.msgs = j.get("msgs", [])
            s.acp_id = j.get("acp_id")
            self.sessions[s.id] = s

    # -- process + JSON-RPC
    def ensure(self):
        with self.lock:
            if self.proc and self.proc.poll() is None and self.state in ("ready", "auth_required"):
                return
            self.start()

    def start(self):
        env = dict(os.environ)
        for k, v in (self.spec.get("env") or {}).items():
            env[k] = expand(v)
        cmd = [expand(c) for c in self.spec["command"]]
        cwd = expand(self.spec.get("cwd", "~"))
        os.makedirs(cwd, exist_ok=True)
        log("starting", self.slug, cmd)
        self.state, self.error = "starting", None
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        self.by_acp.clear()
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._stderr, args=(self.proc,), daemon=True).start()
        try:
            res = self.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "gamut-reaper-gateway", "title": "Gamut REAPER gateway", "version": "0.1.0"},
            }, timeout=120)
        except Exception as e:
            self.state, self.error = "error", str(e)
            raise
        self.caps = res.get("agentCapabilities") or {}
        self.auth_methods = res.get("authMethods") or []
        self.state = "ready"

    def _send(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        with self.wlock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def request(self, method, params, timeout=None):
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            box = {"ev": threading.Event()}
            self.pending[rid] = box
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not box["ev"].wait(timeout):
            self.pending.pop(rid, None)
            raise AcpError(-1, method + " timed out")
        if "error" in box:
            e = box["error"]
            raise AcpError(e.get("code"), e.get("message", "error"), e.get("data"))
        return box.get("result") or {}

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, rid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        if error:
            msg["error"] = error
        else:
            msg["result"] = result
        self._send(msg)

    def _reader(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log(self.slug, "non-json:", line[:200])
                continue
            if "method" not in msg and "id" in msg:
                box = self.pending.pop(msg["id"], None)
                if box:
                    if "error" in msg:
                        box["error"] = msg["error"]
                    else:
                        box["result"] = msg.get("result")
                    box["ev"].set()
            elif "method" in msg and "id" in msg:
                threading.Thread(target=self._on_request, args=(msg,), daemon=True).start()
            elif "method" in msg:
                self._on_notification(msg)
        log(self.slug, "exited", proc.poll())
        if proc is self.proc:
            self.state = "stopped"
            for box in list(self.pending.values()):
                box["error"] = {"code": -1, "message": "agent process exited"}
                box["ev"].set()
            self.pending.clear()

    def _stderr(self, proc):
        for line in proc.stderr:
            log(self.slug, "stderr:", line.decode(errors="replace").rstrip()[:300])

    def _session(self, acp_id):
        return self.by_acp.get(acp_id)

    def _on_notification(self, msg):
        p = msg.get("params") or {}
        if msg["method"] == "session/update":
            s = self._session(p.get("sessionId"))
            if s:
                s.on_update(p.get("update") or {})

    def _on_request(self, msg):
        method, p = msg["method"], msg.get("params") or {}
        if method == "session/request_permission":
            s = self._session(p.get("sessionId"))
            if not s:
                self.respond(msg["id"], {"outcome": {"outcome": "cancelled"}})
                return
            req = str(uuid.uuid4())[:8]
            tc = p.get("toolCall") or {}
            title = tc.get("title") or tc.get("name") or tc.get("kind") or "Run tool"
            ev = {"requestId": req, "title": title, "options": [
                {"optionId": o.get("optionId"), "name": o.get("name"), "kind": o.get("kind")}
                for o in p.get("options") or []]}
            with s.lock:
                s.perms[req] = (msg["id"], ev)
            s.emit("permission_request", **ev)
        else:
            self.respond(msg["id"], error={"code": -32601, "message": "client does not support " + method})

    # -- sessions
    def new_acp_session(self):
        self.ensure()
        cwd = expand(self.spec.get("cwd", "~"))
        try:
            res = self.request("session/new", {"cwd": cwd, "mcpServers": self.mcp_servers}, timeout=180)
        except AcpError as e:
            if e.code == AUTH_REQUIRED:
                self.state, self.connected = "auth_required", False
            raise
        self.state, self.connected = "ready", True
        cfg = normalize_config(res)
        if cfg:
            self.config_cache = cfg
        return res["sessionId"], cfg

    def get_config(self):
        if not self.config_cache:
            acp_id, cfg = self.new_acp_session()
            w = Session(self)
            w.acp_id, w.config = acp_id, cfg
            self.by_acp[acp_id] = w
            self.warm = w
        return self.config_cache

    def create(self, message, config):
        with self.lock:
            s, self.warm = self.warm, None
        if s is None:
            s = Session(self)
            s.acp_id, s.config = self.new_acp_session()
            self.by_acp[s.acp_id] = s
        s.title = message.strip().splitlines()[0][:60] if message.strip() else "New chat"
        self.sessions[s.id] = s
        self.send(s, message, config)
        return s

    def revive(self, s):
        """Reattach a session whose ACP side is gone (gateway or agent restarted)."""
        if s.acp_id and s.acp_id in self.by_acp and self.proc and self.proc.poll() is None:
            return
        self.ensure()
        cwd = expand(self.spec.get("cwd", "~"))
        if s.acp_id and self.caps.get("loadSession"):
            try:
                s.loading = True
                self.by_acp[s.acp_id] = s
                res = self.request("session/load", {"sessionId": s.acp_id, "cwd": cwd,
                                                    "mcpServers": self.mcp_servers}, timeout=180)
                s.config = normalize_config(res) or s.config
                return
            except AcpError as e:
                log(self.slug, "session/load failed:", e.message)
            finally:
                s.loading = False
        s.acp_id, s.config = self.new_acp_session()
        self.by_acp[s.acp_id] = s
        s.emit("notice", text="Started a fresh " + self.name + " session; earlier context is not loaded.")

    def apply_config(self, s, config):
        for cid, value in (config or {}).items():
            opt = next((o for o in s.config if o.get("id") == cid), None)
            if not opt or opt.get("currentValue") == value or value in (None, ""):
                continue
            legacy = opt.get("_legacy")
            if legacy == "model":
                self.request("session/set_model", {"sessionId": s.acp_id, "modelId": value}, timeout=60)
                opt["currentValue"] = value
            elif legacy == "mode":
                self.request("session/set_mode", {"sessionId": s.acp_id, "modeId": value}, timeout=60)
                opt["currentValue"] = value
            else:
                res = self.request("session/set_config_option",
                                   {"sessionId": s.acp_id, "configId": cid, "value": value}, timeout=60)
                if isinstance(res.get("configOptions"), list):
                    s.config = res["configOptions"]
                else:
                    opt["currentValue"] = value
        self.config_cache = s.config or self.config_cache

    def send(self, s, text, config):
        with s.lock:
            if s.active:
                s.backlog.append((text, config))
                return
            s.active = True
        threading.Thread(target=self._turn, args=(s, text, config), daemon=True).start()

    def _turn(self, s, text, config):
        while True:
            try:
                self.revive(s)
                self.apply_config(s, config)
                with s.lock:
                    s.msgs.append({"id": str(uuid.uuid4()), "type": "user", "content": {"text": text},
                                   "createdAt": now_iso()})
                    s.updated = now_iso()
                s.emit("session_active")
                res = self.request("session/prompt", {"sessionId": s.acp_id,
                                                      "prompt": [{"type": "text", "text": text}]})
                if res.get("stopReason") not in (None, "end_turn", "cancelled"):
                    s.emit("notice", text="Stopped: " + str(res.get("stopReason")))
            except AcpError as e:
                with s.lock:
                    s.active = False
                    s.backlog.clear()
                s.updated = now_iso()
                s.persist()
                code = "auth_required" if e.code == AUTH_REQUIRED else "acp_error"
                s.emit("session_error", apiErrorCode=code,
                       errorPresentation={"message": self.name + ": " + e.message})
                return
            with s.lock:
                s.updated = now_iso()
                nxt = s.backlog.pop(0) if s.backlog else None
                if nxt is None:
                    s.active = False
            s.persist()
            if nxt is None:
                s.emit("session_idle")
                return
            text, config = nxt

    def interrupt(self, s):
        with s.lock:
            perms, s.perms = s.perms, {}
            s.backlog.clear()
        for rid, ev in perms.values():
            self.respond(rid, {"outcome": {"outcome": "cancelled"}})
            s.emit("permission_resolved", requestId=ev["requestId"])
        if s.acp_id and self.proc and self.proc.poll() is None:
            self.notify("session/cancel", {"sessionId": s.acp_id})

    def answer(self, s, request_id, option_id):
        with s.lock:
            item = s.perms.pop(request_id, None)
        if not item:
            return False
        rid, _ = item
        if option_id:
            self.respond(rid, {"outcome": {"outcome": "selected", "optionId": option_id}})
        else:
            self.respond(rid, {"outcome": {"outcome": "cancelled"}})
        s.emit("permission_resolved", requestId=request_id)
        return True

    def probe(self):
        """Start the agent and open a warm session so the picker knows whether it is logged in."""
        if self.probing or self.connected:
            return
        self.probing = True
        try:
            self.get_config()
        except Exception as e:  # surfaced through state/error
            if self.state not in ("auth_required",):
                self.error = str(e)
        finally:
            self.probing = False

    def authenticate(self, method_id):
        self.ensure()
        self.state = "authenticating"
        try:
            self.request("authenticate", {"methodId": method_id}, timeout=900)
            self.state, self.error, self.connected = "ready", None, True
        except AcpError as e:
            self.state, self.error = "auth_required", e.message


# ---------------------------------------------------------------- Gamut model selector

EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def gamut_json(path, timeout=10):
    with urllib.request.urlopen(GAMUT + path, timeout=timeout) as r:
        return json.load(r)


def gamut_config():
    """Gamut's model catalog as ACP-style config options; value = '<connectionId>|<modelId>'."""
    conns = gamut_json("/api/llm-connections").get("connections") or []
    models, efforts = [], []
    for c in conns:
        for m in c.get("catalog") or []:
            models.append({"value": c["id"] + "|" + m["id"], "name": m.get("label") or m["id"],
                           "group": c.get("name", ""), "efforts": m.get("supportedEfforts") or []})
    seen = set()
    for m in models:
        for e in m["efforts"]:
            seen.add(e)
    efforts = [e for e in EFFORTS if e in seen]
    out = [{"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": None,
            "options": [{"value": "", "name": "Agent default"}] + models}]
    if efforts:
        out.append({"id": "effort", "name": "Effort", "category": "thought_level", "type": "select",
                    "currentValue": None,
                    "options": [{"value": "", "name": "Default"}] + [{"value": e, "name": e} for e in efforts]})
    return out


def gamut_body(body):
    cfg = body.pop("config", None) or {}
    model = cfg.get("model")
    if model:
        conn, _, mid = model.partition("|")
        body["llmProviderId"], body["model"] = conn, mid
    if cfg.get("effort"):
        body["effort"] = cfg["effort"]
    return body


# ---------------------------------------------------------------- HTTP

class Gateway:
    def __init__(self):
        cfg = load_config()
        mcp = [{"name": m["name"], "command": expand(m["command"]), "args": [expand(a) for a in m.get("args", [])],
                "env": [{"name": k, "value": expand(v)} for k, v in (m.get("env") or {}).items()]}
               for m in cfg.get("mcpServers", [])]
        self.acp = {}
        for spec in cfg.get("acp", []):
            a = AcpAgent(spec, mcp)
            self.acp[a.slug] = a


GW = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("AGENT_GATEWAY_DEBUG"):
            log(fmt % args)

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PATCH(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        path = self.path.split("?", 1)[0]  # acp_route reads the query (?probe=1) from self.path
        parts = [p for p in path.split("/") if p]
        try:
            if path == "/health":
                return self.reply(200, {"ok": True})
            if method == "GET" and parts == ["api", "agents"]:
                return self.agents()
            if len(parts) >= 3 and parts[:2] == ["api", "agents"]:
                slug = parts[2]
                rest = parts[3:]
                if slug in GW.acp:
                    return self.acp_route(method, GW.acp[slug], rest)
                if method == "GET" and rest == ["config"]:
                    return self.reply(200, {"configOptions": gamut_config()})
            return self.proxy(method)
        except AcpError as e:
            code = 401 if e.code == AUTH_REQUIRED else 502
            return self.reply(code, {"error": "auth_required" if code == 401 else e.message, "message": e.message})
        except (BrokenPipeError, ConnectionResetError):
            return None

    def agents(self):
        out = []
        try:
            g = gamut_json("/api/agents", timeout=5)
            out = g.get("agents", g) if isinstance(g, dict) else g
            for a in out:
                a.setdefault("backend", "gamut")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log("gamut unreachable:", e)
        for a in GW.acp.values():
            out.append({"slug": a.slug, "name": a.name, "description": a.spec.get("description", ""),
                        "backend": "acp"})
        self.reply(200, out)

    def acp_route(self, method, a, rest):
        if method == "GET" and rest == ["status"]:
            if "probe=1" in self.path and not a.connected and a.state in ("stopped", "ready", "error"):
                threading.Thread(target=a.probe, daemon=True).start()
            return self.reply(200, {"state": "starting" if a.probing else a.state, "connected": a.connected,
                                    "error": a.error, "authMethods": a.auth_methods})
        if method == "POST" and rest == ["auth"]:
            mid = self.body().get("methodId")
            threading.Thread(target=a.authenticate, args=(mid,), daemon=True).start()
            return self.reply(202, {"state": "authenticating"})
        if method == "GET" and rest == ["config"]:
            return self.reply(200, {"configOptions": a.get_config()})
        if rest == ["sessions"]:
            if method == "GET":
                ss = sorted(a.sessions.values(), key=lambda s: s.updated, reverse=True)
                return self.reply(200, [s.public() for s in ss])
            if method == "POST":
                b = self.body()
                msg = (b.get("message") or "").strip()
                if not msg:
                    return self.reply(400, {"error": "Message is required"})
                s = a.create(msg, b.get("config"))
                return self.reply(201, s.public())
        if len(rest) >= 2 and rest[0] == "sessions":
            s = a.sessions.get(rest[1])
            if not s:
                return self.reply(404, {"error": "Session not found"})
            tail = rest[2:]
            if method == "GET" and tail == ["messages"]:
                with s.lock:
                    return self.reply(200, list(s.msgs))
            if method == "POST" and tail == ["messages"]:
                b = self.body()
                text = (b.get("content") or "").strip()
                if not text:
                    return self.reply(400, {"error": "Content is required"})
                a.send(s, text, b.get("config"))
                return self.reply(200, {"ok": True})
            if method == "GET" and tail == ["stream"]:
                return self.sse(s)
            if method == "POST" and tail == ["interrupt"]:
                a.interrupt(s)
                return self.reply(200, {"ok": True})
            if method == "POST" and tail == ["permission"]:
                b = self.body()
                ok = a.answer(s, b.get("requestId"), b.get("optionId"))
                return self.reply(200 if ok else 404, {"ok": ok})
            if method == "GET" and tail == []:
                return self.reply(200, s.public())
        return self.reply(404, {"error": "not found"})

    def sse(self, s):
        q = queue.Queue()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def write(d):
            self.wfile.write(("event: message\ndata: " + json.dumps(d) + "\n\n").encode())
            self.wfile.flush()

        with s.lock:
            s.subs.add(q)
            pending = [ev for _, ev in s.perms.values()]
        try:
            write({"type": "connected", "isActive": s.active})
            for ev in pending:
                write(dict(ev, type="permission_request"))
            while True:
                try:
                    d = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                write(d)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with s.lock:
                s.subs.discard(q)

    def proxy(self, method):
        body = None
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)
            if method == "POST" and "/sessions" in self.path:
                try:
                    body = json.dumps(gamut_body(json.loads(body))).encode()
                except ValueError:
                    pass
        req = urllib.request.Request(GAMUT + self.path, data=body, method=method)
        for h in ("Content-Type", "Accept"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            resp = urllib.request.urlopen(req, timeout=None if self.path.endswith("/stream") else 60)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except (urllib.error.URLError, OSError) as e:
            return self.reply(502, {"error": "Gamut not reachable: " + str(e)})
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    line = resp.fp.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    if line in (b"\n", b"\r\n"):
                        self.wfile.flush()
            finally:
                resp.close()
            return
        data = resp.read()
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype or "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    global GW
    GW = Gateway()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    log("agent gateway on 127.0.0.1:%d, gamut %s, acp: %s" % (PORT, GAMUT, ", ".join(GW.acp) or "none"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
