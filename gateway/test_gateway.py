#!/usr/bin/env python3
"""End-to-end tests for agent_gateway.py against a fake Gamut API and fake_acp_agent.py. No network, no REAPER."""

import http.client
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
GAMUT_BODIES = []
GAMUT_MCPS = [{"id": "r1", "name": "REAPER", "url": "http://localhost:8787/mcp"}]
GAMUT_ASSIGNED = {}
GAMUT_MSGS = []
GAMUT_PATHS = []


class FakeGamut(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # chunked SSE, like Gamut's Node server

    def log_message(self, *a):
        pass

    def j(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        GAMUT_PATHS.append(self.path)
        if self.path.startswith("/api/agents/g1/sessions/gs1/messages"):
            return self.j(GAMUT_MSGS)
        if self.path == "/api/agents":
            return self.j([{"slug": "g1", "name": "Gamut One"}])
        if self.path == "/api/llm-connections":
            return self.j({"connections": [{"id": "c1", "name": "Conn", "catalog": [
                {"id": "m1", "label": "Model One", "family": "one", "supportedEfforts": ["low", "high"]},
                {"id": "m2", "label": "Model Two", "family": "one", "supportedEfforts": ["low", "high"]}]}]})
        if self.path == "/api/agents/g1/preferences":
            return self.j({"defaultLlmProviderId": "c1", "defaultModel": "one", "defaultEffort": "high"})
        if self.path == "/api/settings":
            return self.j({"models": {"agentModel": "m1", "agentEffort": "low"}})
        if self.path == "/api/remote-mcps":
            return self.j({"servers": GAMUT_MCPS})
        if self.path == "/api/agents/g1/remote-mcps":
            return self.j({"mcps": [m for m in GAMUT_MCPS if m["id"] in GAMUT_ASSIGNED.get("g1", [])]})
        if self.path.endswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for d in ({"type": "connected", "isActive": True}, {"type": "stream_delta", "text": "hi"}):
                chunk = ("event: message\ndata: " + json.dumps(d) + "\n\n").encode()
                for part in (chunk[:10], chunk[10:]):  # an event split across chunks
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.close_connection = True
            return
        self.j({"error": "nope"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path.endswith("/upload-file"):
            ok = (self.headers.get("Content-Type") or "").startswith("multipart/form-data") and b"PNGDATA" in raw
            return self.j({"success": ok, "path": "/workspace/uploads/shot.png"} if ok else {"error": "bad"}, 200 if ok else 400)
        if self.path == "/api/agents/g1/remote-mcps":
            GAMUT_ASSIGNED.setdefault("g1", []).extend(json.loads(raw)["mcpIds"])
            return self.j({"success": True})
        GAMUT_BODIES.append((self.path, json.loads(raw or b"{}")))
        self.j({"id": "gs1"}, 201)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def multipart(filename, data):
    boundary = "gwtestboundary"
    body = ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n" % (boundary, filename)).encode() + data + \
        ("\r\n--%s--\r\n" % boundary).encode()
    return body, "multipart/form-data; boundary=" + boundary


class GW:
    def __init__(self, tmp, gamut_port, acp_env=None, gw_env=None):
        self.port = free_port()
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({
            "acp": [{"slug": "fake", "name": "Fake", "command": [sys.executable, str(HERE / "fake_acp_agent.py")],
                     "env": acp_env or {}, "cwd": tmp, "loginVariants": [
                         {"id": "global", "name": "Log in (global)", "args": ["--region", "global"]},
                         {"id": "cn", "name": "Log in (China)", "args": ["--region", "mainland-cn"]}]}],
            "mcpServers": [{"name": "reaper", "command": sys.executable, "args": ["-c", "pass"]}],
        }))
        env = dict(os.environ, AGENT_GATEWAY_PORT=str(self.port), GAMUT_URL="http://127.0.0.1:%d" % gamut_port,
                   AGENT_GATEWAY_CONFIG=str(cfg), **(gw_env or {}))
        self.proc = subprocess.Popen([sys.executable, str(HERE / "agent_gateway.py")], env=env,
                                     stderr=subprocess.DEVNULL)
        self.auth = {}
        for _ in range(100):
            try:
                self.get("/health")
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("gateway did not start")
        self.token_file = Path(tmp) / "token"
        self.auth = {"Authorization": "Bearer " + self.token_file.read_text().strip()}

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path):
        with urllib.request.urlopen(urllib.request.Request(self.url(path), headers=self.auth), timeout=10) as r:
            return json.load(r)

    def post(self, path, body, headers=None):
        h = dict(self.auth, **{"Content-Type": "application/json"})
        h.update(headers or {})
        req = urllib.request.Request(self.url(path), data=json.dumps(body).encode(), method="POST", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def upload(self, path, filename, data):
        body, ctype = multipart(filename, data)
        req = urllib.request.Request(self.url(path), data=body, method="POST", headers=dict(self.auth, **{"Content-Type": ctype}))
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def stream(self, path):
        q = queue.Queue()

        def run():
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            c.request("GET", path, headers=self.auth)
            r = c.getresponse()
            while True:
                line = r.readline()
                if not line:
                    break
                if line.startswith(b"data: "):
                    q.put(json.loads(line[6:]))
                elif line.strip() and not line.startswith((b"event:", b":")):
                    q.put({"type": "garbage", "line": line.decode(errors="replace")})
        threading.Thread(target=run, daemon=True).start()
        return q

    def stop(self):
        self.proc.terminate()
        self.proc.wait(5)


def until(q, kind, on=None, timeout=10):
    seen = []
    end = time.time() + timeout
    while time.time() < end:
        try:
            d = q.get(timeout=end - time.time())
        except queue.Empty:
            break
        seen.append(d)
        if on:
            on(d)
        if d["type"] == kind:
            return seen
    raise AssertionError("no %s in %s" % (kind, [d["type"] for d in seen]))


def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        raise SystemExit(1)


def unit():
    sys.path.insert(0, str(HERE))
    import agent_gateway as ag
    check("title drops the context block",
          ag.chat_title("[REAPER context — x]\nproject: a\n[/REAPER context]\n\nmake it brighter") == "make it brighter")
    check("title drops a pasted, escaped context block",
          ag.chat_title("\\[REAPER context — x\\] p \\[/REAPER context\\]\n\nyo") == "yo")
    a = ag.AcpAgent.__new__(ag.AcpAgent)
    a.spec, a.cwd = {"slug": "kimi", "command": ["/nonexistent/kimi", "acp"], "resume": ["no-such-cli-xyz", "{id}"]}, "/tmp"
    check("no resume line without the CLI", a.resume_cmd("abc") is None)
    a.spec["resume"] = ["sh", "-c", "echo {id}"]
    line = a.resume_cmd("abc") or ""
    if os.name == "nt":
        check("resume line runs in the agent's cwd", line.startswith('cd /d "/tmp" && ') and '"echo abc"' in line or print(line))
    else:
        check("resume line runs in the agent's cwd", line.startswith("cd /tmp && ") and "echo abc" in line)
    a.spec["env"] = {"PATH": "/opt/node/bin"}
    line = a.resume_cmd("abc") or ""
    if os.name == "nt":
        check("resume line carries the agent's PATH", 'set "PATH=/opt/node/bin;%PATH%" && ' in line or print(line))
    else:
        check("resume line carries the agent's PATH", "PATH=/opt/node/bin:\"$PATH\" exec " in line)
    ag.WINDOWS = True
    try:
        line = ag.shell_line(["C:\\n\\kimi.cmd", "--session", "s_1"], env={"K": "v w"}, cwd="C:\\Users\\x\\My Songs",
                             path_prefix="C:\\n")
    finally:
        ag.WINDOWS = os.name == "nt"
    check("windows terminal line is cmd syntax",
          line == 'cd /d "C:\\Users\\x\\My Songs" && set "PATH=C:\\n;%PATH%" && set "K=v w" && "C:\\n\\kimi.cmd" "--session" "s_1"' or print(line))
    ag.WINDOWS = True
    try:
        pct = ag.shell_line(["a%PATH%b"], env={"K": "100%"})
        refused = []
        for argv, env in ((['x" & calc & "'], None), (["ok"], {"K": 'v" & calc'}), (["a\r\nb"], None), (["ok"], {"A B": "v"})):
            try:
                ag.shell_line(argv, env=env)
            except ValueError:
                refused.append(True)
    finally:
        ag.WINDOWS = os.name == "nt"
    check("windows: % doubled so cmd doesn't expand it", pct == 'set "K=100%%" && "a%%PATH%%b"' or print(pct))
    check("windows: quotes, line breaks and odd env names refused", len(refused) == 4)
    a.spec = {"slug": "x", "command": ["/bin/sh"], "resume": ["sh", "{id}"]}
    check("resume refuses an unsafe session id", a.resume_cmd("a;rm -rf ~") is None)
    check("safe ids", ag.safe_id("acp-kimi") and ag.safe_id("6f1c.2_x") and not ag.safe_id("..")
          and not ag.safe_id("a/b") and not ag.safe_id("a b") and not ag.safe_id(""))


def unit_terminal_login():
    sys.path.insert(0, str(HERE))
    import agent_gateway as ag
    spec = {"command": ["mcode", "acp"], "terminalLogin": {"id": "login", "name": "Log in", "args": ["login"]},
            "loginVariants": [{"id": "global", "name": "Log in (global)", "args": ["--region", "global"]},
                              {"id": "cn", "name": "Log in (China)", "args": []}]}
    ms = ag.login_variants([], spec)
    check("no declared auth method: terminalLogin becomes one button per region",
          [m["id"] for m in ms] == ["login:global", "login:cn"])
    check("terminal login argv = command + login + region", ag.terminal_auth(ms[0], spec)[0] == ["mcode", "acp", "login", "--region", "global"]
          and ag.terminal_auth(ms[1], spec)[0] == ["mcode", "acp", "login"])
    check("declared methods win over terminalLogin", ag.login_variants([{"id": "grok.com"}], spec) == [{"id": "grok.com"}])


def unit_install():
    sys.path.insert(0, str(HERE))
    import agent_gateway as ag
    tmp = Path(tempfile.mkdtemp())
    ag.CONFIG, ag.STATE = tmp / "config.json", tmp / "sessions"
    ag.CONFIG.write_text(json.dumps({"acp": [{"slug": "one", "command": ["x"]}], "mcpServers": []}))
    g = ag.Gateway.__new__(ag.Gateway)
    g.acp = {}
    g.reload()
    first = g.acp["acp-one"]

    def fake_install(key):  # what agents_add does to the config
        cfg = json.loads(ag.CONFIG.read_text())
        cfg["acp"].append({"slug": key, "command": ["y"]})
        ag.CONFIG.write_text(json.dumps(cfg))

    inst = ag.Installs(run=fake_install)
    check("install starts", inst.start("kimi", g.reload))
    for _ in range(100):
        if inst.state["kimi"]["state"] != "installing":
            break
        time.sleep(0.02)
    check("install: new agent loaded in place, running ones kept",
          inst.state["kimi"]["state"] == "done" and "acp-kimi" in g.acp and g.acp["acp-one"] is first)

    def boom(key):
        raise SystemExit("Node download failed its sha256 check")
    inst2 = ag.Installs(run=boom)
    inst2.start("codex", g.reload)
    for _ in range(100):
        if inst2.state["codex"]["state"] != "installing":
            break
        time.sleep(0.02)
    check("install: a failure is reported, not raised", inst2.state["codex"] == {"state": "error",
          "error": "Node download failed its sha256 check"})


def main():
    unit()
    unit_terminal_login()
    unit_install()
    gsrv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGamut)
    threading.Thread(target=gsrv.serve_forever, daemon=True).start()
    gport = gsrv.server_address[1]
    tmp = tempfile.mkdtemp()
    g = GW(tmp, gport)
    try:
        agents = g.get("/api/agents")
        check("agents merge gamut + acp", [a["slug"] for a in agents] == ["g1", "acp-fake"])

        cfg = g.get("/api/agents/g1/config")["configOptions"]
        check("gamut model selector", cfg[0]["options"][1]["value"] == "c1|m1" and cfg[1]["id"] == "effort")
        check("gamut defaults resolved (family -> newest, agent prefs win)",
              cfg[0]["currentValue"] == "c1|m2" and cfg[1]["currentValue"] == "high")
        code, _ = g.post("/api/agents/g1/sessions", {"message": "hi", "config": {"model": "c1|m1", "effort": "high"}})
        body = GAMUT_BODIES[-1][1]
        g.post("/api/agents/g1/sessions", {"message": "hi", "title": "hi"})
        check("title hint not forwarded to Gamut", GAMUT_BODIES[-1][1] == {"message": "hi"})
        check("gamut config -> llmProviderId/model/effort",
              code == 201 and body == {"message": "hi", "llmProviderId": "c1", "model": "m1", "effort": "high"})
        q = g.stream("/api/agents/g1/sessions/gs1/stream")
        check("gamut chunked SSE proxied clean", [d["type"] for d in until(q, "stream_delta")] == ["connected", "stream_delta"])

        # Windows has no mode bits (NTFS ACLs; the file sits in the user's profile)
        inst = g.get("/api/hibiki/installable")
        check("hibiki: installable lists codex, claude, kimi, minimax, grok (none installed here)",
              [x["key"] for x in inst] == ["codex", "claude", "kimi", "minimax", "grok"] and not any(x["installed"] for x in inst))
        st = g.get("/api/hibiki/gamut-reaper?agent=g1")
        check("hibiki: REAPER registered in Gamut, not yet enabled for g1", st == {"registered": True, "enabled": False})
        code, r = g.post("/api/hibiki/gamut-reaper", {"agent": "g1"})
        check("hibiki: enable assigns the MCP to the agent", code == 200 and r["status"] == "enabled"
              and GAMUT_ASSIGNED.get("g1") == ["r1"])
        check("hibiki: then reads as enabled", g.get("/api/hibiki/gamut-reaper?agent=g1")["enabled"] is True)
        code, r = g.post("/api/hibiki/install", {"agent": "nope"})
        check("hibiki: unknown install refused", code == 400)

        check("token file is private", g.token_file.exists() if os.name == "nt"
              else os.stat(g.token_file).st_mode & 0o777 == 0o600)
        code, _ = g.post("/api/agents/acp-fake/sessions", {"message": "hi"}, {"Authorization": ""})
        check("missing token refused", code == 403)
        code, _ = g.post("/api/agents/acp-fake/sessions", {"message": "hi"}, {"Host": "host.docker.internal:47892",
                                                                             "Authorization": "Bearer wrong"})
        check("container host with a wrong token refused", code == 403)
        code, _ = g.post("/api/agents/acp-fake/sessions", {"message": "hi"}, {"Origin": "https://evil.example"})
        check("browser Origin refused", code == 403)
        code, _ = g.post("/api/agents/acp-fake/sessions", {"message": "hi"}, {"Host": "evil.example"})
        check("foreign Host refused", code == 403)

        GAMUT_MSGS[:] = [{"id": "m1", "type": "user"}, {"id": "m2", "type": "assistant"}, {"id": "m3", "type": "user"}]
        base = "/api/agents/g1/sessions/gs1/messages"
        check("gamut messages without after: proxied as is", g.get(base) == GAMUT_MSGS)
        check("gamut messages?after=m1", g.get(base + "?after=m1") == {"after": "m1", "messages": GAMUT_MSGS[1:]})
        check("gamut messages?after=m3", g.get(base + "?after=m3") == {"after": "m3", "messages": []})
        check("gamut messages?after=<unknown>: full", g.get(base + "?after=zz") == {"full": True, "messages": GAMUT_MSGS})
        check("gamut never sees the after param", all("after=" not in p_ for p_ in GAMUT_PATHS))

        try:
            g.get("/api/agents/..%2F..%2Fapi%2Fsettings/sessions")
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        check("agent slug outside [A-Za-z0-9._-] refused", code == 400)
        try:
            g.get("/api/agents/g1/sessions/a%20b/messages")
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        check("session id outside [A-Za-z0-9._-] refused", code == 400)

        c = socket.create_connection(("127.0.0.1", g.port), timeout=10)
        c.sendall(b"POST /api/agents/acp-fake/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"Content-Length: 50000000\r\n\r\n")
        t0 = time.time()
        resp = c.recv(4096)
        c.close()
        check("unauthenticated 50 MB body refused from the headers, before any of it is read",
              resp.startswith(b"HTTP/1.1 403") and time.time() - t0 < 5)
        c = socket.create_connection(("127.0.0.1", g.port), timeout=10)
        c.sendall(b"POST /api/agents/acp-fake/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"Authorization: " + g.auth["Authorization"].encode() + b"\r\nContent-Length: 999999999\r\n\r\n")
        resp = c.recv(4096)
        c.close()
        check("oversized Content-Length refused before reading", resp.startswith(b"HTTP/1.1 413"))

        st = g.get("/api/agents/acp-fake/status?probe=1")
        time.sleep(0.5)
        st2 = g.get("/api/agents/acp-fake/status")
        check("probe=1 is a cheap check: nothing spawned", st["running"] is False and st2["running"] is False
              and st2["state"] == "stopped" and not st2["connected"])
        g.get("/api/agents/acp-fake/status?probe=acp-fake")
        for _ in range(100):
            st = g.get("/api/agents/acp-fake/status")
            if st["connected"]:
                break
            time.sleep(0.05)
        check("probe=<slug> starts that agent and connects", st["connected"] and st["state"] == "ready" and st["running"])
        acfg = g.get("/api/agents/acp-fake/config")["configOptions"]
        check("acp model selector", acfg[0]["category"] == "model" and acfg[0]["currentValue"] == "fast")
        code, s = g.post("/api/agents/acp-fake/sessions", {"message": "[REAPER context]\n#1 Drums\n[/REAPER context]\n\n"
                                                           "use tool please", "title": "use tool please",
                                                           "config": {"model": "smart"}})
        check("acp session titled from the panel's title, not the context block",
              code == 201 and s["name"] == "use tool please")
        sid = s["id"]
        time.sleep(0.5)  # subscribe late on purpose: the opening delta and tool call must be replayed
        q = g.stream("/api/agents/acp-fake/sessions/%s/stream" % sid)

        def allow(d):
            if d["type"] == "permission_request":
                g.post("/api/agents/acp-fake/sessions/%s/permission" % sid, {"requestId": d["requestId"],
                                                                           "optionId": "allow"})
        seen = until(q, "session_idle", allow)
        kinds = [d["type"] for d in seen]
        check("acp events", {"stream_delta", "tool_use_ready", "permission_request", "permission_resolved"} <= set(kinds))
        msgs = g.get("/api/agents/acp-fake/sessions/%s/messages" % sid)
        text = msgs[-1]["content"]["text"]
        check("acp transcript", msgs[0]["type"] == "user" and "[smart]" in text and "permission=allow" in text
              and msgs[-1]["toolCalls"][0]["name"] == "Edit REAPER project")
        base = "/api/agents/acp-fake/sessions/%s/messages" % sid
        inc = g.get(base + "?after=" + msgs[0]["id"])
        check("acp messages?after=<id>: only newer messages", inc == {"after": msgs[0]["id"], "messages": msgs[1:]})
        check("acp messages?after=<last id>: empty", g.get(base + "?after=" + msgs[-1]["id"]) == {"after": msgs[-1]["id"],
                                                                                                 "messages": []})
        check("acp messages?after=<unknown>: full list flagged", g.get(base + "?after=nope") == {"full": True, "messages": msgs})
        check("acp messages without after: plain array as before", isinstance(g.get(base), list))

        g.post("/api/agents/acp-fake/sessions/%s/messages" % sid, {"content": "tool again"})

        def cancel(d):
            if d["type"] == "permission_request":
                g.post("/api/agents/acp-fake/sessions/%s/interrupt" % sid, {})
        until(q, "session_idle", cancel)
        msgs = g.get("/api/agents/acp-fake/sessions/%s/messages" % sid)
        check("interrupt cancels permission", "permission=cancelled" in msgs[-1]["content"]["text"])
        check("sessions listed", [x["id"] for x in g.get("/api/agents/acp-fake/sessions")] == [sid])

        code, r = g.upload("/api/agents/g1/upload-file", "shot.png", b"\x89PNGDATA")
        check("gamut upload proxied as multipart", code == 200 and r["path"] == "/workspace/uploads/shot.png")
        body, ctype = multipart("x.txt", b"hi")
        req = urllib.request.Request(g.url("/api/agents/acp-fake/sessions"), data=body, method="POST",
                                     headers=dict(g.auth, **{"Content-Type": ctype}))
        try:
            urllib.request.urlopen(req, timeout=10)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        check("multipart refused outside upload routes", code == 403)
        code, img = g.upload("/api/agents/acp-fake/upload-file", "shot.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        code2, doc = g.upload("/api/agents/acp-fake/sessions/%s/upload-file" % sid, "notes.txt", b"hello")
        check("acp uploads saved", code == 200 and code2 == 200 and os.path.isfile(img["path"])
              and open(doc["path"], "rb").read() == b"hello")
        _, doc2 = g.upload("/api/agents/acp-fake/upload-file", "notes.txt", b"other take")
        check("same-name uploads in the same second don't overwrite", doc2["path"] != doc["path"]
              and open(doc["path"], "rb").read() == b"hello")
        for _ in range(100):  # let the previous turn finish so this message isn't queued behind it
            if not g.get("/api/agents/acp-fake/sessions/%s" % sid)["isActive"]:
                break
            time.sleep(0.05)
        before = len(g.get("/api/agents/acp-fake/sessions/%s/messages" % sid))
        g.post("/api/agents/acp-fake/sessions/%s/messages" % sid,
               {"content": "look\n\n[Attached files:]\n- %s\n- %s" % (img["path"], doc["path"])})
        msgs = []
        for _ in range(100):  # poll the transcript for this turn's user message + reply
            msgs = g.get("/api/agents/acp-fake/sessions/%s/messages" % sid)
            if len(msgs) >= before + 2 and msgs[-1]["type"] == "assistant" and " echo: look" in msgs[-1]["content"]["text"]:
                break
            time.sleep(0.05)
        check("attachments become image + resource_link blocks",
              "blocks=text,image,resource_link" in msgs[-1]["content"]["text"])
    finally:
        g.stop()

    g = GW(tmp, gport)
    try:
        msgs = g.get("/api/agents/acp-fake/sessions/%s/messages" % sid)
        check("transcript survives restart", len(msgs) >= 4 and msgs[0]["type"] == "user")
        q = g.stream("/api/agents/acp-fake/sessions/%s/stream" % sid)
        g.post("/api/agents/acp-fake/sessions/%s/messages" % sid, {"content": "after restart"})
        seen = until(q, "session_idle")
        check("revive starts fresh ACP session", "notice" in [d["type"] for d in seen])
    finally:
        g.stop()

    tmp3 = tempfile.mkdtemp()
    g = GW(tmp3, gport, {"FAKE_ACP_NOISE": "1"})
    try:
        code, s = g.post("/api/agents/acp-fake/sessions", {"message": "please crash"})
        q = g.stream("/api/agents/acp-fake/sessions/%s/stream" % s["id"])
        seen = until(q, "session_error")
        check("noise ignored, crash mid-turn ends the turn", code == 201 and seen[-1]["type"] == "session_error")
        check("session not stuck after crash", g.get("/api/agents/acp-fake/sessions/%s" % s["id"])["isActive"] is False)
        g.post("/api/agents/acp-fake/sessions/%s/messages" % s["id"], {"content": "again"})
        seen = until(q, "session_idle")
        check("agent restarts on next message", "notice" in [d["type"] for d in seen])

        code, s = g.post("/api/agents/acp-fake/sessions", {"message": "tool then crash"})
        sid3 = s["id"]
        q = g.stream("/api/agents/acp-fake/sessions/%s/stream" % sid3)

        def queue_then_allow(d):
            if d["type"] == "permission_request":
                g.post("/api/agents/acp-fake/sessions/%s/messages" % sid3, {"content": "queued follow-up"})
                g.post("/api/agents/acp-fake/sessions/%s/permission" % sid3, {"requestId": d["requestId"],
                                                                            "optionId": "allow"})
        seen = until(q, "session_error", queue_then_allow)
        msgs = g.get("/api/agents/acp-fake/sessions/%s/messages" % sid3)
        check("queued message kept in the transcript when the turn fails",
              "1 queued message not sent" in seen[-1]["errorPresentation"]["message"]
              and msgs[-1]["type"] == "user" and msgs[-1]["content"]["text"] == "queued follow-up")
        late = until(g.stream("/api/agents/acp-fake/sessions/%s/stream" % sid3), "session_error")
        check("late subscriber gets the finished turn's error", [d["type"] for d in late] == ["connected", "session_error"])
    finally:
        g.stop()

    tmp2 = tempfile.mkdtemp()
    g = GW(tmp2, gport, {"FAKE_ACP_AUTH": "1"})
    try:
        g.get("/api/agents/acp-fake/status?probe=fake")
        for _ in range(100):
            st = g.get("/api/agents/acp-fake/status")
            if st["state"] == "auth_required":
                break
            time.sleep(0.05)
        check("probe finds auth required", st["state"] == "auth_required" and not st["connected"])
        code, body = g.post("/api/agents/acp-fake/sessions", {"message": "hi"})
        st = g.get("/api/agents/acp-fake/status")
        check("auth required surfaced", code == 401 and body["error"] == "auth_required"
              and st["state"] == "auth_required" and st["authMethods"][0]["id"] == "login")
        g.post("/api/agents/acp-fake/auth", {"methodId": "login"})
        for _ in range(50):
            if g.get("/api/agents/acp-fake/status")["state"] == "ready":
                break
            time.sleep(0.05)
        code, _ = g.post("/api/agents/acp-fake/sessions", {"message": "hi"})
        check("login then session", code == 201 and g.get("/api/agents/acp-fake/status")["connected"])
    finally:
        g.stop()

    tmp3 = tempfile.mkdtemp()
    mark = os.path.join(tmp3, "logged-in")
    g = GW(tmp3, gport, {"FAKE_ACP_AUTH": "terminal", "FAKE_ACP_MARK": mark},
           {"AGENT_GATEWAY_TERMINAL": "cmd.exe /d /c" if os.name == "nt" else "sh -c", "AGENT_GATEWAY_LOGIN_POLL": "0.1"})
    try:
        code, _ = g.post("/api/agents/acp-fake/sessions", {"message": "hi"})
        st = g.get("/api/agents/acp-fake/status")
        check("terminal login split into variants", code == 401 and [m["id"] for m in st["authMethods"]]
              == ["login:global", "login:cn"] and st["authMethods"][1]["name"] == "Log in (China)")
        g.post("/api/agents/acp-fake/auth", {"methodId": "login:global"})
        for _ in range(100):
            st = g.get("/api/agents/acp-fake/status")
            if st["connected"]:
                break
            time.sleep(0.05)
        check("terminal login runs the command and connects", open(mark).read() == "--region global" and st["connected"]
              and st["state"] == "ready")
        code, _ = g.post("/api/agents/acp-fake/sessions", {"message": "hi"})
        check("session after terminal login", code == 201)
    finally:
        g.stop()

    tmp4 = tempfile.mkdtemp()
    kids = os.path.join(tmp4, "kids")
    g = GW(tmp4, gport, {"FAKE_ACP_CHILD": kids},
           {"AGENT_GATEWAY_IDLE": "1", "AGENT_GATEWAY_IDLE_CHECK": "0.2", "AGENT_GATEWAY_MAX_REQUESTS": "1"})
    try:
        code, s = g.post("/api/agents/acp-fake/sessions", {"message": "hi"})
        sid4 = s["id"]
        for _ in range(100):
            if not g.get("/api/agents/acp-fake/sessions/%s" % sid4)["isActive"]:
                break
            time.sleep(0.05)
        check("idle test: agent ran a turn", code == 201 and g.get("/api/agents/acp-fake/status")["running"])
        q = g.stream("/api/agents/acp-fake/sessions/%s/stream" % sid4)
        until(q, "connected")
        time.sleep(1.6)
        check("idle reaper leaves an agent with an open stream alone", g.get("/api/agents/acp-fake/status")["running"])
        g.stop()
        g = GW(tmp4, gport, {"FAKE_ACP_CHILD": kids},
               {"AGENT_GATEWAY_IDLE": "1", "AGENT_GATEWAY_IDLE_CHECK": "0.2", "AGENT_GATEWAY_MAX_REQUESTS": "1"})
        code, s = g.post("/api/agents/acp-fake/sessions", {"message": "hi"})
        pids = [tuple(map(int, ln.split())) for ln in open(kids).read().splitlines() if ln.strip()]
        agent_pid, child_pid = pids[-1]
        st = {}
        for _ in range(100):
            st = g.get("/api/agents/acp-fake/status")
            if not st["running"]:
                break
            time.sleep(0.05)
        check("idle reaper stops an idle agent; last login state still shown",
              st["running"] is False and st["connected"] is True and st["state"] == "ready")
        if os.name != "nt":
            def alive(pid):
                try:
                    os.kill(pid, 0)
                except OSError:
                    return False
                try:  # a zombie still answers kill(0); it has exited
                    return open("/proc/%d/stat" % pid).read().split(")")[-1].split()[0] != "Z"
                except OSError:
                    return True
            time.sleep(0.5)
            check("idle stop takes the agent's MCP children with it", not alive(agent_pid) and not alive(child_pid))
        q = g.stream("/api/agents/acp-fake/sessions/%s/stream" % s["id"])
        g.post("/api/agents/acp-fake/sessions/%s/messages" % s["id"], {"content": "back again"})
        seen = until(q, "session_idle")
        check("a stopped agent restarts lazily on the next message",
              "notice" in [d["type"] for d in seen] and g.get("/api/agents/acp-fake/status")["running"])

        stall = socket.create_connection(("127.0.0.1", g.port), timeout=10)
        stall.sendall(b"POST /api/agents/acp-fake/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                      b"Authorization: " + g.auth["Authorization"].encode() + b"\r\nContent-Length: 10\r\n\r\n")
        time.sleep(0.3)
        try:
            g.get("/api/agents")
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        check("requests over the in-flight cap get 503", code == 503)
        stall.close()
        time.sleep(0.3)
        check("the slot frees when the stalled request ends", isinstance(g.get("/api/agents"), list))
    finally:
        g.stop()
    print("all passed")


if __name__ == "__main__":
    main()
