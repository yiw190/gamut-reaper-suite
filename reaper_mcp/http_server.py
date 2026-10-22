#!/usr/bin/env python3
"""Bearer-protected Streamable HTTP front for reaper-mcp stdio."""
from __future__ import annotations

import hmac
import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reaper_mcp import server as mcp_server  # noqa: E402

PORT = int(os.environ.get("REAPER_MCP_PORT", "8787"))
TOKEN_FILE = Path(os.environ.get("REAPER_MCP_TOKEN_FILE", "~/.config/reaper-mcp/token")).expanduser()
MIN_BEARER = 32
SHORT_TIMEOUT = float(os.environ.get("REAPER_MCP_SHORT_TIMEOUT", "30"))
CALL_TIMEOUT = float(os.environ.get("REAPER_MCP_CALL_TIMEOUT", "600"))
LOCAL_METHODS = ("initialize", "tools/list", "ping")


def _bearer() -> str:
    if os.environ.get("REAPER_MCP_BEARER"):
        return os.environ["REAPER_MCP_BEARER"]
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


BEARER = _bearer()
SERVER = Path(__file__).resolve().parent / "server.py"


MAX_BODY = 16 * 1024 * 1024


def rpc_timeout(msg: dict) -> float:
    if msg.get("method") != "tools/call":
        return SHORT_TIMEOUT
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    extra = 0.0
    try:
        if params.get("name") == "render":
            extra = mcp_server.RENDER_TIMEOUT
        elif params.get("name") == "batch" and isinstance(args.get("calls"), list):
            extra = mcp_server._normalize_batch(args["calls"])[1] or 0.0
    except Exception:  # noqa: BLE001
        extra = 0.0
    return CALL_TIMEOUT + extra


class StdioClient:
    def __init__(self, cmd: list | None = None) -> None:
        self.cmd = cmd or [sys.executable, str(SERVER)]
        self.proc = None
        self.lines = None
        self.lock = threading.Lock()

    def _start(self) -> None:
        # stderr goes to our (launchd) log so tracebacks aren't lost and a full pipe can't block the child
        self.proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self.lines), daemon=True).start()

    @staticmethod
    def _pump(proc, lines) -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)

    def _kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def rpc(self, msg: dict, timeout: float | None = None):
        if msg.get("method") in LOCAL_METHODS and msg.get("id") is not None:
            return mcp_server.handle_request(None, msg)
        if msg.get("id") is None and str(msg.get("method") or "").startswith("notifications/"):
            return None
        rid = msg.get("id")
        line = json.dumps(msg, ensure_ascii=False)
        wait = rpc_timeout(msg) if timeout is None else timeout
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                if self.proc is not None:
                    sys.stderr.write("stdio MCP exited (%s); restarting\n" % self.proc.returncode)
                self._start()
            assert self.proc.stdin and self.lines is not None
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
            if rid is None:
                return None
            deadline = time.monotonic() + wait
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    sys.stderr.write("stdio MCP gave no reply to id %r in %.0fs; restarting it\n" % (rid, wait))
                    self._kill()
                    raise TimeoutError("no reply from the MCP server in %.0fs; it was restarted" % wait)
                try:
                    out = self.lines.get(timeout=min(left, 1.0))
                except queue.Empty:
                    continue
                if out is None:
                    self._kill()
                    raise RuntimeError("stdio MCP closed stdout")
                out = out.strip()
                if not out:
                    continue
                try:
                    resp = json.loads(out)
                except ValueError:
                    sys.stderr.write("stdio MCP wrote a non-JSON line; dropped\n")
                    continue
                if isinstance(resp, dict) and resp.get("id") == rid and ("result" in resp or "error" in resp):
                    return resp
                sys.stderr.write("dropped a stdio MCP line that doesn't answer id %r\n" % (rid,))


CLIENT = StdioClient()  # the child starts on the first request


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self) -> None:
        self._send(
            401,
            b'{"error":"unauthorized"}',
            "application/json",
            {"WWW-Authenticate": 'Bearer realm="reaper-mcp"'},
        )

    def _auth_ok(self) -> bool:
        if not BEARER:
            return False
        header = self.headers.get("Authorization", "")
        return hmac.compare_digest(header.encode(), f"Bearer {BEARER}".encode())

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] in ("/healthz", "/health"):
            self._send(200, b'{"ok":true}', "application/json")
            return
        if not self._auth_ok():
            self._unauthorized()
            return
        self._send(405, b'{"error":"use POST"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/", "/mcp"):
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        if not self._auth_ok():
            self._unauthorized()
            return
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            self._send(413, b'{"error":"bad Content-Length"}', "application/json")
            return
        raw = self.rfile.read(n) if n else b"{}"
        try:
            msg = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._send(400, b'{"error":"invalid json"}', "application/json")
            return
        if not isinstance(msg, dict):
            self._send(400, b'{"error":"send one JSON-RPC request object"}', "application/json")
            return
        try:
            result = CLIENT.rpc(msg)
        except Exception as e:  # noqa: BLE001
            err = json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(e)}})
            self._send(200, err.encode("utf-8"), "application/json")
            return
        if result is None:
            self._send(202, b"", "application/json")
            return
        self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json")


def main() -> None:
    if not BEARER:
        raise SystemExit("no bearer: set REAPER_MCP_BEARER or write %s" % TOKEN_FILE)
    if len(BEARER) < MIN_BEARER:
        raise SystemExit("bearer is %d characters; use at least %d (e.g. `python3 -c 'import secrets; "
                         "print(secrets.token_urlsafe(32))'` into %s, or run `hibiki setup` to generate one)"
                         % (len(BEARER), MIN_BEARER, TOKEN_FILE))
    if not SERVER.is_file():
        raise SystemExit(f"missing {SERVER}")
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"reaper-mcp http on 127.0.0.1:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
