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


class FakeGamut(BaseHTTPRequestHandler):
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
        if self.path == "/api/agents":
            return self.j([{"slug": "g1", "name": "Gamut One"}])
        if self.path == "/api/llm-connections":
            return self.j({"connections": [{"id": "c1", "name": "Conn", "catalog": [
                {"id": "m1", "label": "Model One", "supportedEfforts": ["low", "high"]}]}]})
        if self.path.endswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for d in ({"type": "connected", "isActive": True}, {"type": "stream_delta", "text": "hi"}):
                self.wfile.write(("event: message\ndata: " + json.dumps(d) + "\n\n").encode())
                self.wfile.flush()
            return
        self.j({"error": "nope"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        GAMUT_BODIES.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
        self.j({"id": "gs1"}, 201)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class GW:
    def __init__(self, tmp, gamut_port, acp_env=None):
        self.port = free_port()
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({
            "acp": [{"slug": "fake", "name": "Fake", "command": [sys.executable, str(HERE / "fake_acp_agent.py")],
                     "env": acp_env or {}, "cwd": tmp}],
            "mcpServers": [{"name": "reaper", "command": sys.executable, "args": ["-c", "pass"]}],
        }))
        env = dict(os.environ, AGENT_GATEWAY_PORT=str(self.port), GAMUT_URL="http://127.0.0.1:%d" % gamut_port,
                   AGENT_GATEWAY_CONFIG=str(cfg))
        self.proc = subprocess.Popen([sys.executable, str(HERE / "agent_gateway.py")], env=env,
                                     stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                self.get("/health")
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("gateway did not start")

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as r:
            return json.load(r)

    def post(self, path, body):
        req = urllib.request.Request(self.url(path), data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def stream(self, path):
        q = queue.Queue()

        def run():
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            c.request("GET", path)
            r = c.getresponse()
            while True:
                line = r.fp.readline()
                if not line:
                    break
                if line.startswith(b"data: "):
                    q.put(json.loads(line[6:]))
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


def main():
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
        code, _ = g.post("/api/agents/g1/sessions", {"message": "hi", "config": {"model": "c1|m1", "effort": "high"}})
        body = GAMUT_BODIES[-1][1]
        check("gamut config -> llmProviderId/model/effort",
              code == 201 and body == {"message": "hi", "llmProviderId": "c1", "model": "m1", "effort": "high"})
        q = g.stream("/api/agents/g1/sessions/gs1/stream")
        check("gamut SSE proxied", [d["type"] for d in until(q, "stream_delta")] == ["connected", "stream_delta"])

        g.get("/api/agents/acp-fake/status?probe=1")
        for _ in range(100):
            st = g.get("/api/agents/acp-fake/status")
            if st["connected"]:
                break
            time.sleep(0.05)
        check("probe connects", st["connected"] and st["state"] == "ready")
        acfg = g.get("/api/agents/acp-fake/config")["configOptions"]
        check("acp model selector", acfg[0]["category"] == "model" and acfg[0]["currentValue"] == "fast")
        code, s = g.post("/api/agents/acp-fake/sessions", {"message": "use tool please", "config": {"model": "smart"}})
        check("acp session created", code == 201 and s["name"] == "use tool please")
        sid = s["id"]
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

        g.post("/api/agents/acp-fake/sessions/%s/messages" % sid, {"content": "tool again"})

        def cancel(d):
            if d["type"] == "permission_request":
                g.post("/api/agents/acp-fake/sessions/%s/interrupt" % sid, {})
        until(q, "session_idle", cancel)
        msgs = g.get("/api/agents/acp-fake/sessions/%s/messages" % sid)
        check("interrupt cancels permission", "permission=cancelled" in msgs[-1]["content"]["text"])
        check("sessions listed", [x["id"] for x in g.get("/api/agents/acp-fake/sessions")] == [sid])
    finally:
        g.stop()

    g = GW(tmp, gport)
    try:
        msgs = g.get("/api/agents/acp-fake/sessions/%s/messages" % sid)
        check("transcript survives restart", len(msgs) == 4)
        q = g.stream("/api/agents/acp-fake/sessions/%s/stream" % sid)
        g.post("/api/agents/acp-fake/sessions/%s/messages" % sid, {"content": "after restart"})
        seen = until(q, "session_idle")
        check("revive starts fresh ACP session", "notice" in [d["type"] for d in seen])
    finally:
        g.stop()

    tmp2 = tempfile.mkdtemp()
    g = GW(tmp2, gport, {"FAKE_ACP_AUTH": "1"})
    try:
        g.get("/api/agents/acp-fake/status?probe=1")
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
    print("all passed")


if __name__ == "__main__":
    main()
