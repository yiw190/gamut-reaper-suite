#!/usr/bin/env python3
"""Agent gateway: one local REST + SSE API for the REAPER chat panel.

Gamut agents are forwarded to the Gamut desktop API (POST bodies' "config" is turned into Gamut's
model/llmProviderId/effort; GET /config is generated from Gamut's model catalog). ACP agents (Codex, Claude Code,
Gemini CLI, ...) run as stdio subprocesses and are served under the same routes, as agents with slug `acp-<slug>`:

  GET  /api/agents                                   Gamut agents + ACP agents
  GET  /api/agents/<slug>/config                     model / effort / mode selectors (normalized config options)
  GET  /api/agents/<slug>/status[?probe=1]           ACP: {state, connected, authMethods, error}; probe starts it
  POST /api/agents/<slug>/auth          {methodId}   ACP: start login (terminal methods open Terminal.app)
  GET  /api/agents/<slug>/sessions
  POST /api/agents/<slug>/sessions      {message, config}
  GET  /api/agents/<slug>/sessions/<id>
  POST /api/agents/<slug>/sessions/<id>/messages {content, config}
  GET  /api/agents/<slug>/sessions/<id>/messages
  GET  /api/agents/<slug>/sessions/<id>/stream       SSE: connected, session_active, stream_delta, tool_use_ready,
                                                     session_idle, session_error, notice, permission_request,
                                                     permission_resolved, config_options
  POST /api/agents/<slug>/sessions/<id>/interrupt
  POST /api/agents/<slug>/sessions/<id>/permission {requestId, optionId|null}
  POST /api/agents/<slug>[/sessions/<id>]/upload-file   multipart "file" -> {success, path, filename, size}

Attachments use Gamut's convention: the message ends with "[Attached files:]" and "- <path>" lines. For ACP agents
the gateway also sends each file as a prompt block (an image block when the agent accepts images, otherwise a
resource_link).

Stdlib only; runs on macOS' /usr/bin/python3 (3.9). Listens on 127.0.0.1 only and refuses browser requests
(any Origin header) and foreign Host headers, so web pages can't drive local agents.
"""

import base64
import email
import email.policy
import json
import mimetypes
import os
import queue
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
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
# localhost plus the names container runtimes give the host (Lima, Docker, Podman)
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "host.docker.internal", "host.lima.internal", "host.containers.internal"}
SSE_QUEUE_MAX = 2000
MAX_UPLOAD = 50 * 1024 * 1024
MAX_IMAGE_BLOCK = 10 * 1024 * 1024
ATTACHED = "[Attached files:]"  # Gamut's attachment block, appended to a message as "- <path>" lines
LOGIN_POLL = float(os.environ.get("AGENT_GATEWAY_LOGIN_POLL", "3"))
LOGIN_TIMEOUT = 900


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


def open_terminal(command):
    """Run a shell command in a visible terminal (Terminal.app; AGENT_GATEWAY_TERMINAL overrides the launcher)."""
    custom = os.environ.get("AGENT_GATEWAY_TERMINAL")
    if custom:
        subprocess.Popen(shlex.split(custom) + [command], stdin=subprocess.DEVNULL)
        return
    esc = command.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.Popen(["osascript", "-e", 'tell application "Terminal"', "-e", "activate",
                      "-e", 'do script "%s"' % esc, "-e", "end tell"], stdin=subprocess.DEVNULL)


def terminal_auth(method, spec):
    """argv + env for an ACP auth method that wants the client to run a login command in a terminal."""
    meta = (method.get("_meta") or {}).get("terminal-auth")
    if isinstance(meta, dict) and meta.get("command"):
        argv, env = [meta["command"]] + list(meta.get("args") or []), meta.get("env") or {}
    elif method.get("type") == "terminal":
        argv, env = [expand(c) for c in spec["command"]] + list(method.get("args") or []), method.get("env") or {}
    else:
        return None
    argv += [str(a) for a in method.get("loginArgs") or []]
    merged = dict(spec.get("env") or {})
    merged.update(env)
    return argv, merged


def login_variants(methods, spec):
    """Split each terminal login into the config's loginVariants (e.g. Kimi: one button per region)."""
    variants = spec.get("loginVariants") or []
    out = []
    for m in methods:
        if not variants or not terminal_auth(m, {"command": spec["command"]}):
            out.append(m)
            continue
        for v in variants:
            out.append(dict(m, id="%s:%s" % (m.get("id"), v["id"]), name=v.get("name", v["id"]),
                            description=v.get("description", m.get("description")),
                            loginArgs=[str(a) for a in v.get("args") or []]))
    return out


class AcpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def parse_multipart(content_type, body):
    """[(field name, filename, bytes)] for the file parts of a multipart/form-data body."""
    msg = email.message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body,
                                   policy=email.policy.HTTP)
    out = []
    for part in msg.iter_parts():
        name = part.get_filename()
        if name:
            out.append((part.get_param("name", header="content-disposition"), name, part.get_payload(decode=True) or b""))
    return out


def attached_paths(text):
    i = text.find(ATTACHED)
    if i < 0:
        return []
    paths = []
    for line in text[i + len(ATTACHED):].splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("- "):
            break
        paths.append(line[2:].strip())
    return paths


def prompt_blocks(text, caps):
    blocks = [{"type": "text", "text": text}]
    images = bool((caps.get("promptCapabilities") or {}).get("image"))
    for path in attached_paths(text):
        if not os.path.isfile(path):
            continue
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if images and mime.startswith("image/") and os.path.getsize(path) <= MAX_IMAGE_BLOCK:
            with open(path, "rb") as f:
                blocks.append({"type": "image", "mimeType": mime, "data": base64.b64encode(f.read()).decode()})
        else:
            blocks.append({"type": "resource_link", "uri": "file://" + urllib.parse.quote(path),
                           "name": os.path.basename(path), "mimeType": mime})
    return blocks


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
        self.cancelled = False
        self.subs = set()
        self.perms = {}  # requestId -> (JSON-RPC id, event)
        self.config = []
        self.backlog = []
        self.loading = False
        self.lock = threading.RLock()

    def emit(self, kind, **data):
        data["type"] = kind
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:  # a client that stopped reading; its SSE loop drops it
                q.dead = True

    def public(self):
        return {"id": self.id, "name": self.title or "New chat", "isActive": self.active,
                "lastActivityAt": self.updated, "createdAt": self.created}

    def assistant(self):
        if not self.msgs or self.msgs[-1]["type"] != "assistant":
            self.msgs.append({"id": str(uuid.uuid4()), "type": "assistant", "content": {"text": ""},
                              "toolCalls": [], "createdAt": now_iso()})
        return self.msgs[-1]

    def tool_title(self, tool_call_id):
        with self.lock:
            for m in reversed(self.msgs):
                for tc in m.get("toolCalls") or []:
                    if tc.get("id") == tool_call_id:
                        return tc.get("name")
        return None

    def drop_perms(self):
        with self.lock:
            perms, self.perms = self.perms, {}
        for _, ev in perms.values():
            self.emit("permission_resolved", requestId=ev["requestId"])
        return perms

    def persist(self):
        p = STATE / self.agent.slug / (self.id + ".json")
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock:
            data = json.dumps({"id": self.id, "acp_id": self.acp_id, "title": self.title,
                               "created": self.created, "updated": self.updated, "msgs": self.msgs})
        tmp = p.with_name(p.name + "." + uuid.uuid4().hex[:8] + ".tmp")
        tmp.write_text(data)
        tmp.replace(p)

    def on_update(self, u):
        if self.loading or not isinstance(u, dict):
            return
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            c = u.get("content") or {}
            if c.get("type") == "text" and c.get("text"):
                with self.lock:
                    self.assistant()["content"]["text"] += c["text"]
                self.emit("stream_delta", text=c["text"])
        elif kind == "tool_call":
            title = u.get("title") or u.get("kind") or "tool"
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
        self.cwd = os.path.abspath(expand(spec.get("cwd", "~")))
        self.mcp_servers = mcp_servers
        self.proc = None
        self.lock = threading.RLock()      # process lifecycle + request ids
        self.wlock = threading.Lock()      # stdin writes
        self.cfg_lock = threading.Lock()   # warm session / config cache
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
        self.config_cache = None  # None = not fetched yet; [] = agent has no options
        self.warm = None
        self.load_persisted()

    def load_persisted(self):
        d = STATE / self.slug
        if not d.is_dir():
            return
        for p in d.glob("*.json"):
            try:
                j = json.loads(p.read_text())
                s = Session(self, p.stem, j.get("title", ""), j.get("created"))
                s.updated = j.get("updated", s.created)
                s.msgs = j.get("msgs") if isinstance(j.get("msgs"), list) else []
                s.acp_id = j.get("acp_id")
            except Exception as e:
                log(self.slug, "skipping", p.name, e)
                continue
            self.sessions[s.id] = s

    # -- process + JSON-RPC
    def ensure(self):
        with self.lock:
            if self.proc and self.proc.poll() is None and self.state not in ("error", "stopped"):
                return
            self.start()

    def _fail_pending(self, message):
        pending, self.pending = self.pending, {}
        for box in pending.values():
            box["error"] = {"code": -1, "message": message}
            box["ev"].set()

    def start(self):
        old = self.proc
        if old and old.poll() is None:
            old.terminate()
            try:
                old.wait(3)
            except subprocess.TimeoutExpired:
                old.kill()
        self._fail_pending("agent restarted")
        env = dict(os.environ)
        for k, v in (self.spec.get("env") or {}).items():
            env[k] = expand(v)
        cmd = [expand(c) for c in self.spec["command"]]
        os.makedirs(self.cwd, exist_ok=True)
        log("starting", self.slug, cmd)
        self.state, self.error, self.connected = "starting", None, False
        self.by_acp.clear()
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
        except OSError as e:
            self.proc, self.state, self.error = None, "error", str(e)
            raise AcpError(-1, "cannot start %s: %s" % (self.name, e))
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._stderr, args=(self.proc,), daemon=True).start()
        try:
            res = self.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "gamut-reaper-gateway", "title": "Gamut REAPER gateway", "version": "0.1.0"},
            }, timeout=120)
            if res.get("protocolVersion") != 1:
                raise AcpError(-1, "%s speaks ACP protocol %s, gateway speaks 1" % (self.name, res.get("protocolVersion")))
        except AcpError as e:
            self.state, self.error = "error", e.message
            self.proc.terminate()
            raise
        self.caps = res.get("agentCapabilities") or {}
        self.auth_methods = login_variants(res.get("authMethods") or [], self.spec)
        self.state = "ready"

    def _send(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        proc = self.proc
        try:
            with self.wlock:
                proc.stdin.write(data)
                proc.stdin.flush()
        except (AttributeError, OSError, ValueError):
            raise AcpError(-1, self.name + " is not running")

    def request(self, method, params, timeout=None):
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            box = {"ev": threading.Event()}
            self.pending[rid] = box
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except AcpError:
            self.pending.pop(rid, None)
            raise
        if not box["ev"].wait(timeout):
            self.pending.pop(rid, None)
            raise AcpError(-1, method + " timed out")
        if "error" in box:
            e = box["error"]
            raise AcpError(e.get("code"), e.get("message", "error"))
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
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError("not an object")
                    self._dispatch(msg)
                except Exception as e:  # one bad line must not kill the reader
                    log(self.slug, "bad message:", e, line[:200])
        finally:
            log(self.slug, "exited", proc.poll())
            if proc is self.proc:
                self.state, self.connected = "stopped", False
                self._fail_pending(self.name + " exited")
                for s in list(self.sessions.values()) + ([self.warm] if self.warm else []):
                    s.drop_perms()

    def _dispatch(self, msg):
        if "method" not in msg and "id" in msg:
            box = self.pending.pop(msg["id"], None)
            if box:
                if "error" in msg:
                    box["error"] = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                else:
                    box["result"] = msg.get("result")
                box["ev"].set()
        elif "method" in msg and "id" in msg:
            self._on_request(msg)
        elif msg.get("method") == "session/update":
            p = msg.get("params") or {}
            s = self.by_acp.get(p.get("sessionId"))
            if s:
                s.on_update(p.get("update") or {})

    def _stderr(self, proc):
        for line in proc.stderr:
            log(self.slug, "stderr:", line.decode(errors="replace").rstrip()[:300])

    def _on_request(self, msg):
        method, p = msg["method"], msg.get("params") or {}
        if method != "session/request_permission":
            self.respond(msg["id"], error={"code": -32601, "message": "client does not support " + method})
            return
        s = self.by_acp.get(p.get("sessionId"))
        if not s:
            self.respond(msg["id"], {"outcome": {"outcome": "cancelled"}})
            return
        tc = p.get("toolCall") or {}
        title = tc.get("title") or s.tool_title(tc.get("toolCallId")) or tc.get("kind") or "Run tool"
        ev = {"requestId": uuid.uuid4().hex[:8], "title": title, "options": [
            {"optionId": o.get("optionId"), "name": o.get("name"), "kind": o.get("kind")}
            for o in p.get("options") or []]}
        with s.lock:
            s.perms[ev["requestId"]] = (msg["id"], ev)
            s.emit("permission_request", **ev)

    # -- sessions
    def open_session(self, s):
        """Give `s` a fresh ACP session (starting the agent if needed)."""
        self.ensure()
        try:
            res = self.request("session/new", {"cwd": self.cwd, "mcpServers": self.mcp_servers}, timeout=180)
        except AcpError as e:
            if e.code == AUTH_REQUIRED:
                self.state, self.connected = "auth_required", False
            raise
        if not res.get("sessionId"):
            raise AcpError(-1, self.name + " returned no sessionId")
        self.state, self.connected = "ready", True
        s.acp_id, s.config = res["sessionId"], normalize_config(res)
        self.config_cache = s.config
        self.by_acp[s.acp_id] = s

    def get_config(self):
        with self.cfg_lock:
            if self.config_cache is None:
                w = Session(self)
                self.open_session(w)
                self.warm = w
            return self.config_cache

    def create(self, message, config):
        with self.cfg_lock:
            s, self.warm = self.warm, None
        if s is None:
            s = Session(self)
            self.open_session(s)
        s.title = message.strip().splitlines()[0][:60] if message.strip() else "New chat"
        self.sessions[s.id] = s
        self.send(s, message, config)
        return s

    def revive(self, s):
        """Reattach a session whose ACP side is gone (gateway or agent restarted)."""
        if s.acp_id and s.acp_id in self.by_acp and self.proc and self.proc.poll() is None:
            return
        self.ensure()
        if s.acp_id and self.caps.get("loadSession"):
            s.loading = True
            self.by_acp[s.acp_id] = s
            try:
                res = self.request("session/load", {"sessionId": s.acp_id, "cwd": self.cwd,
                                                    "mcpServers": self.mcp_servers}, timeout=180)
                s.config = normalize_config(res) or s.config
                return
            except AcpError as e:
                log(self.slug, "session/load failed:", e.message)
                self.by_acp.pop(s.acp_id, None)
            finally:
                s.loading = False
        self.open_session(s)
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
            s.active, s.cancelled = True, False
        threading.Thread(target=self._turn, args=(s, text, config), daemon=True).start()

    def _turn(self, s, text, config):
        while True:
            try:
                self.revive(s)
                try:
                    self.apply_config(s, config)
                except AcpError as e:  # a stale pick must not cost the message
                    s.emit("notice", text="Kept the previous settings: " + e.message)
                with s.lock:
                    s.msgs.append({"id": str(uuid.uuid4()), "type": "user", "content": {"text": text},
                                   "createdAt": now_iso()})
                    s.updated = now_iso()
                s.emit("session_active")
                if not s.cancelled:
                    res = self.request("session/prompt", {"sessionId": s.acp_id,
                                                          "prompt": prompt_blocks(text, self.caps)})
                    if res.get("stopReason") not in (None, "end_turn", "cancelled"):
                        s.emit("notice", text="Stopped: " + str(res.get("stopReason")))
            except Exception as e:
                code, message = (e.code, e.message) if isinstance(e, AcpError) else (-1, repr(e))
                if code == AUTH_REQUIRED:
                    self.state, self.connected = "auth_required", False
                with s.lock:
                    s.active = False
                    s.backlog.clear()
                    s.updated = now_iso()
                s.drop_perms()
                self._save(s)
                s.emit("session_error", apiErrorCode="auth_required" if code == AUTH_REQUIRED else "acp_error",
                       errorPresentation={"message": self.name + ": " + message})
                return
            s.drop_perms()
            with s.lock:
                s.updated = now_iso()
                nxt = s.backlog.pop(0) if s.backlog else None
                if nxt is None:
                    s.active = False
                else:
                    s.cancelled = False
            self._save(s)
            if nxt is None:
                s.emit("session_idle")
                return
            text, config = nxt

    def _save(self, s):
        try:
            s.persist()
        except OSError as e:
            log(self.slug, "could not save transcript:", e)

    def interrupt(self, s):
        with s.lock:
            s.backlog.clear()
            s.cancelled = s.active
        for rid, _ in s.drop_perms().values():
            try:
                self.respond(rid, {"outcome": {"outcome": "cancelled"}})
            except AcpError:
                pass
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

    def save_upload(self, filename, data):
        d = os.path.join(self.cwd, "uploads")
        os.makedirs(d, exist_ok=True)
        safe = re.sub(r"[^\w.\- ]+", "_", os.path.basename(filename)).strip() or "file"
        path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S-") + safe)
        with open(path, "wb") as f:
            f.write(data)
        return {"success": True, "path": path, "filename": filename, "size": len(data)}

    def probe(self):
        """Start the agent and open a warm session so the picker knows whether it is logged in."""
        with self.lock:
            if self.probing or self.connected:
                return
            self.probing = True
        try:
            self.get_config()
        except Exception as e:  # surfaced through state/error
            if self.state != "auth_required":
                self.error = str(e)
        finally:
            self.probing = False

    def authenticate(self, method_id):
        method = next((m for m in self.auth_methods if m.get("id") == method_id), {})
        term = terminal_auth(method, self.spec)
        if term:
            return self.terminal_login(*term)
        try:
            self.ensure()
            self.state = "authenticating"
            self.request("authenticate", {"methodId": method_id}, timeout=900)
            self.state, self.error, self.connected = "ready", None, True
        except AcpError as e:
            self.state, self.error = "auth_required", e.message

    def terminal_login(self, argv, env):
        """Open the agent's login command in a terminal, then retry a session until the login lands."""
        exports = "".join("export %s=%s; " % (k, shlex.quote(str(expand(v)))) for k, v in env.items())
        self.state, self.error = "authenticating", None
        try:
            open_terminal(exports + " ".join(shlex.quote(a) for a in argv))
        except OSError as e:
            self.state, self.error = "auth_required", "cannot open a terminal: %s" % e
            return
        deadline = time.time() + LOGIN_TIMEOUT
        while time.time() < deadline:
            time.sleep(LOGIN_POLL)
            try:
                with self.cfg_lock:
                    self.config_cache = None
                self.get_config()
                return
            except AcpError as e:
                if e.code != AUTH_REQUIRED:
                    self.state, self.error = "auth_required", e.message
                    return
                self.state = "authenticating"
        self.state, self.error = "auth_required", "login timed out"


# ---------------------------------------------------------------- Gamut model selector

EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def gamut_json(path, timeout=10):
    with urllib.request.urlopen(GAMUT + path, timeout=timeout) as r:
        return json.load(r)


def gamut_defaults(slug):
    """What the agent runs with when nothing is picked: its own preferences, else Gamut's global defaults."""
    prefs, glob = {}, {}
    try:
        prefs = gamut_json("/api/agents/%s/preferences" % urllib.parse.quote(slug, safe="")) or {}
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        glob = (gamut_json("/api/settings") or {}).get("models") or {}
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return (prefs.get("defaultLlmProviderId"), prefs.get("defaultModel") or glob.get("agentModel"),
            prefs.get("defaultEffort") or glob.get("agentEffort"))


def resolve_model(conns, conn_id, model):
    """'<connectionId>|<modelId>' for a model id or family alias ("opus" = the family's newest entry)."""
    if not model:
        return None
    for c in [c for c in conns if c.get("id") == conn_id] or conns:
        cat = c.get("catalog") or []
        for m in cat:
            if m.get("id") == model:
                return c["id"] + "|" + m["id"]
        family = [m for m in cat if m.get("family") == model]
        if family:
            return c["id"] + "|" + family[-1]["id"]
    return None


def gamut_config(slug=None):
    """Gamut's model catalog as ACP-style config options; value = '<connectionId>|<modelId>'.
    currentValue is what the agent uses when nothing is picked, so the panel can show it."""
    conns = gamut_json("/api/llm-connections").get("connections") or []
    conn_id, model_default, effort_default = gamut_defaults(slug) if slug else (None, None, None)
    models = []
    for c in conns:
        for m in c.get("catalog") or []:
            models.append({"value": c["id"] + "|" + m["id"], "name": m.get("label") or m["id"],
                           "group": c.get("name", ""), "efforts": m.get("supportedEfforts") or []})
    seen = {e for m in models for e in m["efforts"]}
    efforts = [e for e in EFFORTS if e in seen]
    out = [{"id": "model", "name": "Model", "category": "model", "type": "select",
            "currentValue": resolve_model(conns, conn_id, model_default),
            "options": [{"value": "", "name": "Agent default"}] + models}]
    if efforts:
        out.append({"id": "effort", "name": "Effort", "category": "thought_level", "type": "select",
                    "currentValue": effort_default if effort_default in efforts else None,
                    "options": [{"value": "", "name": "Default"}] + [{"value": e, "name": e} for e in efforts]})
    return out


def gamut_body(body):
    cfg = body.pop("config", None)
    cfg = cfg if isinstance(cfg, dict) else {}
    model = cfg.get("model")
    if isinstance(model, str) and model:
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
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PATCH(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")

    def refuse(self):
        """Only local, non-browser clients: no Origin, a host name we expect, JSON bodies."""
        if self.headers.get("Origin") is not None:
            return "browser requests are not accepted"
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ALLOWED_HOSTS:
            return "unexpected Host"
        ctype = self.headers.get("Content-Type") or ""
        upload = self.path.split("?", 1)[0].endswith("/upload-file")
        if self.raw and "json" not in ctype and not (upload and ctype.startswith("multipart/form-data")):
            return "JSON body required"
        return None

    def route(self, method):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_UPLOAD:
            self.raw, self.json = b"", {}
            return self.reply(413, {"error": "bad or too large Content-Length"})
        self.raw = self.rfile.read(n) if n > 0 else b""
        try:
            self.json = json.loads(self.raw) if self.raw else {}
        except ValueError:
            self.json = {}
        if not isinstance(self.json, dict):
            self.json = {}
        path = self.path.split("?", 1)[0]  # acp_route reads the query (?probe=1) from self.path
        parts = [p for p in path.split("/") if p]
        try:
            why = self.refuse()
            if why:
                return self.reply(403, {"error": why})
            if not path.startswith("/"):
                return self.reply(400, {"error": "bad path"})
            if path == "/health":
                return self.reply(200, {"ok": True})
            if method == "GET" and parts == ["api", "agents"]:
                return self.agents()
            if len(parts) >= 3 and parts[:2] == ["api", "agents"]:
                slug, rest = parts[2], parts[3:]
                if slug in GW.acp:
                    return self.acp_route(method, GW.acp[slug], rest)
                if method == "GET" and rest == ["config"]:
                    return self.reply(200, {"configOptions": gamut_config(slug)})
            return self.proxy(method, path)
        except AcpError as e:
            code = 401 if e.code == AUTH_REQUIRED else 502
            return self.reply(code, {"error": "auth_required" if code == 401 else e.message, "message": e.message})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return None
        except (urllib.error.URLError, OSError) as e:
            return self.reply(502, {"error": "Gamut not reachable: " + str(e)})
        except Exception as e:
            log("unhandled", method, self.path, repr(e))
            try:
                return self.reply(500, {"error": repr(e)})
            except OSError:
                return None

    def agents(self):
        out = []
        try:
            g = gamut_json("/api/agents", timeout=5)
            g = g.get("agents", []) if isinstance(g, dict) else g
            out = [a for a in g if isinstance(a, dict)] if isinstance(g, list) else []
            for a in out:
                a.setdefault("backend", "gamut")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log("gamut unreachable:", e)
        for a in GW.acp.values():
            out.append({"slug": a.slug, "name": a.name, "description": a.spec.get("description", ""),
                        "backend": "acp"})
        self.reply(200, out)

    def acp_route(self, method, a, rest):
        b = self.json
        if method == "GET" and rest == ["status"]:
            if "probe=1" in self.path and not a.connected and a.state in ("stopped", "ready", "error"):
                threading.Thread(target=a.probe, daemon=True).start()
            return self.reply(200, {"state": "starting" if a.probing else a.state, "connected": a.connected,
                                    "error": a.error, "authMethods": a.auth_methods})
        if method == "POST" and rest == ["auth"]:
            threading.Thread(target=a.authenticate, args=(b.get("methodId"),), daemon=True).start()
            return self.reply(202, {"state": "authenticating"})
        if method == "GET" and rest == ["config"]:
            return self.reply(200, {"configOptions": a.get_config()})
        if method == "POST" and rest[-1:] == ["upload-file"] and (len(rest) == 1 or rest[0] == "sessions"):
            files = parse_multipart(self.headers.get("Content-Type") or "", self.raw)
            f = next((x for x in files if x[0] == "file"), files[0] if files else None)
            if not f:
                return self.reply(400, {"error": "No file provided"})
            return self.reply(200, a.save_upload(f[1], f[2]))
        if rest == ["sessions"]:
            if method == "GET":
                ss = sorted(a.sessions.values(), key=lambda s: s.updated, reverse=True)
                return self.reply(200, [s.public() for s in ss])
            if method == "POST":
                msg = str(b.get("message") or "").strip()
                if not msg:
                    return self.reply(400, {"error": "Message is required"})
                return self.reply(201, a.create(msg, b.get("config")).public())
        if len(rest) >= 2 and rest[0] == "sessions":
            s = a.sessions.get(rest[1])
            if not s:
                return self.reply(404, {"error": "Session not found"})
            tail = rest[2:]
            if method == "GET" and tail == []:
                return self.reply(200, s.public())
            if method == "GET" and tail == ["messages"]:
                with s.lock:
                    msgs = list(s.msgs)
                return self.reply(200, msgs)
            if method == "POST" and tail == ["messages"]:
                text = str(b.get("content") or "").strip()
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
                ok = a.answer(s, b.get("requestId"), b.get("optionId"))
                return self.reply(200 if ok else 404, {"ok": ok})
        return self.reply(404, {"error": "not found"})

    def sse(self, s):
        q = queue.Queue(SSE_QUEUE_MAX)
        q.dead = False
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(30)  # a client that stops reading can't pin this thread forever

        def write(d):
            self.wfile.write(("event: message\ndata: " + json.dumps(d) + "\n\n").encode())
            self.wfile.flush()

        with s.lock:  # subscribe and snapshot together so nothing is missed or doubled
            s.subs.add(q)
            pending = [ev for _, ev in s.perms.values()]
            active = s.active
            # a client that subscribes after the turn started (the panel creates the session, then subscribes)
            # gets the reply so far; "connected" with isActive tells it to rebuild its live view from this
            turn = s.msgs[-1] if active and s.msgs and s.msgs[-1]["type"] == "assistant" else None
            replay_text = turn["content"]["text"] if turn else ""
            replay_tools = [tc.get("name") for tc in turn.get("toolCalls") or []] if turn else []
        try:
            write({"type": "connected", "isActive": active})
            for name in replay_tools:
                write({"type": "tool_use_ready", "toolName": name})
            if replay_text:
                write({"type": "stream_delta", "text": replay_text})
            for ev in pending:
                write(dict(ev, type="permission_request"))
            while not q.dead:
                try:
                    d = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                write(d)
        except (OSError, socket.timeout):
            pass
        finally:
            with s.lock:
                s.subs.discard(q)

    def proxy(self, method, path):
        body = self.raw or None
        if body and method == "POST" and "/sessions" in path and self.json:
            body = json.dumps(gamut_body(dict(self.json))).encode()
        stream = path.endswith("/stream")
        req = urllib.request.Request(GAMUT + self.path, data=body, method=method)
        for h in ("Content-Type", "Accept"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            resp = urllib.request.urlopen(req, timeout=None if stream else 60)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(data)
            return
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
                    line = resp.readline()  # HTTPResponse.readline decodes chunked transfer encoding
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
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
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
