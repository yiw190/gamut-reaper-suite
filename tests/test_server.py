#!/usr/bin/env python3
"""Offline MCP + fake-bridge tests. No REAPER required."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "reaper_mcp", "server.py")
BRIDGE = os.path.join(ROOT, "reaper_mcp", "bridge.lua")

sys.path.insert(0, ROOT)


def check_invalid_utf8_response():
    """A mailbox reply with illegal UTF-8 must not look like a dead bridge."""
    from reaper_mcp import server

    bridge_dir = tempfile.mkdtemp(prefix="reaper_utf8_")
    prev = os.environ.get("REAPER_MCP_IPC_DIR")
    os.environ["REAPER_MCP_IPC_DIR"] = bridge_dir
    try:
        b = server.Bridge()
        with open(b.heart, "w", encoding="utf-8") as f:
            f.write("1")

        def writer():
            deadline = time.time() + 3
            while time.time() < deadline:
                if os.path.exists(b.req):
                    with open(b.req, encoding="utf-8") as f:
                        payload = json.load(f)
                    try:
                        os.remove(b.req)
                    except OSError:
                        pass
                    rid = payload.get("id", "")
                    raw = (
                        '{"id":"%s","ok":true,"ret":{"project":"波","tracks":[{"name":"Qq'
                        % rid
                    ).encode("utf-8") + bytes([0xD4]) + b'z"}]}}'
                    with open(b.resp, "wb") as f:
                        f.write(raw)
                    return
                time.sleep(0.01)

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        ret = b.call("get_project_summary", timeout=3)
        t.join(timeout=1)
        assert ret["project"] == "波", ret
        assert ret["tracks"][0]["name"].startswith("Qq"), ret
        return True
    finally:
        if prev is None:
            os.environ.pop("REAPER_MCP_IPC_DIR", None)
        else:
            os.environ["REAPER_MCP_IPC_DIR"] = prev


def check_batch_timeout():
    """A batch containing a render must inherit the render budget."""
    from reaper_mcp import server

    calls, timeout = server._normalize_batch([
        {"func": "track", "action": "add", "name": "Pad"},
    ])
    assert calls == [{"func": "add_track", "args": ["Pad", None]}], calls
    assert timeout is None, timeout

    calls, timeout = server._normalize_batch([
        {"func": "track", "action": "add", "name": "Pad"},
        {"func": "render", "arguments": {"path": "/tmp/x.wav"}},
    ])
    assert timeout == server.RENDER_TIMEOUT, timeout

    calls, timeout = server._normalize_batch([{"func": "render_project", "args": ["/tmp/x.wav"]}])
    assert timeout == server.RENDER_TIMEOUT, timeout

    calls, timeout = server._normalize_batch([
        {"func": "midi", "action": "cc", "track_index": 0, "item_index": 0,
         "ccs": [{"start_beats": 0, "program": 0}]},
        {"func": "item", "action": "import", "path": "/tmp/a.mid", "track_index": 0},
        {"func": "project", "action": "save", "path": "/tmp/song.RPP"},
    ])
    assert calls[0]["func"] == "add_midi_cc", calls[0]
    assert calls[1]["func"] == "import_media", calls[1]
    assert calls[1]["args"][0] == "/tmp/a.mid", calls[1]
    assert calls[1]["args"][1]["as_new_track"] is False, calls[1]
    assert calls[2] == {"func": "save_project", "args": ["/tmp/song.RPP"]}, calls[2]
    return True

FAKE_CHILD = r"""
import json, sys, time
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "hang":
        time.sleep(60)
    if msg.get("id") is None:
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": "stray", "result": {}}) + "\n")
    sys.stdout.write("not json\n")
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"echo": msg.get("method")}}) + "\n")
    sys.stdout.flush()
"""


def check_http_front(env):
    """The HTTP front's stdio client: matches replies by id, times out and restarts, answers tools/list unlocked."""
    from reaper_mcp import http_server

    results = {}
    fake = http_server.StdioClient([sys.executable, "-c", FAKE_CHILD])
    r = fake.rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "status"}})
    results["stray lines dropped, reply matched by id"] = r == {"jsonrpc": "2.0", "id": 7, "result": {"echo": "tools/call"}}
    first = fake.proc
    t0 = time.monotonic()
    try:
        fake.rpc({"jsonrpc": "2.0", "id": 8, "method": "hang"}, timeout=1.0)
        results["hung child times out"] = False
    except TimeoutError:
        results["hung child times out"] = time.monotonic() - t0 < 5
    results["hung child killed"] = first.poll() is not None
    r = fake.rpc({"jsonrpc": "2.0", "id": 9, "method": "x"})
    results["next call restarts the child"] = r["id"] == 9 and fake.proc is not first

    def hang():
        try:
            fake.rpc({"jsonrpc": "2.0", "id": 10, "method": "hang"}, timeout=3.0)
        except TimeoutError:
            pass
    hold = threading.Thread(target=hang, daemon=True)
    hold.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    tl = fake.rpc({"jsonrpc": "2.0", "id": 11, "method": "tools/list"})
    init = fake.rpc({"jsonrpc": "2.0", "id": 12, "method": "initialize", "params": {}})
    results["tools/list + initialize answered while a call holds the lock"] = (
        time.monotonic() - t0 < 1 and len(tl["result"]["tools"]) >= 8 and tl["id"] == 11
        and init["result"]["serverInfo"]["name"] == "reaper-mcp")
    results["notifications answered locally"] = fake.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    hold.join()
    fake._kill()

    old_env = dict(os.environ)
    os.environ.update(env)
    try:
        real = http_server.StdioClient()
        real.rpc({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "status", "arguments": {}}})
        real.rpc({"jsonrpc": "2.0", "method": "ping"})
        r = real.rpc({"jsonrpc": "2.0", "id": "a1", "method": "tools/call", "params": {"name": "status", "arguments": {}}})
        results["id-less calls then an id'd call: the right reply"] = r.get("id") == "a1" and "Drums" in r["result"]["content"][0]["text"]
        r = real.rpc({"jsonrpc": "2.0", "id": "n1", "method": "notifications/cancelled"}, timeout=5)
        results["notification carrying an id is answered, not hung"] = r == {"jsonrpc": "2.0", "id": "n1", "result": {}}
        real._kill()
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    import subprocess as sp
    short = sp.run([sys.executable, os.path.join(ROOT, "reaper_mcp", "http_server.py")], capture_output=True, text=True,
                   env=dict(os.environ, REAPER_MCP_BEARER="short", REAPER_MCP_PORT="0"), timeout=30)
    results["short bearer refused at startup"] = short.returncode != 0 and "at least 32" in short.stderr
    return results


def fake_bridge(bridge_dir, stop):
    req = os.path.join(bridge_dir, "request.json")
    resp = os.path.join(bridge_dir, "response.json")
    heart = os.path.join(bridge_dir, "heartbeat")
    busy = os.path.join(bridge_dir, "busy")
    while not stop.is_set():
        with open(heart, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        if os.path.exists(req):
            try:
                with open(req, encoding="utf-8") as f:
                    payload = json.load(f)
                os.remove(req)
            except (OSError, ValueError):
                time.sleep(0.005)
                continue
            func = payload.get("func")
            if func == "run_lua" and (payload.get("code") or "") == "slow":
                with open(busy, "w", encoding="utf-8") as f:
                    f.write("1")
                time.sleep(0.6)
                try:
                    os.remove(busy)
                except OSError:
                    pass
            if func == "get_project_summary":
                detail = bool((payload.get("args") or [False])[0])
                ret = {"project": "(unsaved)", "tempo_bpm": 120.0,
                       "track_count": 1,
                       "tracks": [{"index": 0, "name": "Drums"}]}
                if detail:
                    ret["time_sig"] = {"numerator": 4, "denominator": 4}
                    ret["length_beats"] = 32.0
                    ret["cursor_beats"] = 0.0
                    ret["markers"] = []
                    ret["tracks"][0]["has_midi"] = True
                    ret["tracks"][0]["has_audio"] = False
            elif func == "add_track":
                ret = {"index": payload["args"][1] or 0,
                       "name": payload["args"][0] or ""}
            elif func == "run_lua":
                ret = 1
            elif func == "batch":
                calls = payload["args"][0]
                ret = [{"echo": c.get("func"), "args": c.get("args")}
                       for c in calls]
            elif func == "CountTracks":
                ret = 3
            else:
                ret = {"echo": func, "args": payload.get("args")}
            out = {"id": payload.get("id"), "ok": True, "ret": ret}
            tmp = resp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f)
            os.replace(tmp, resp)
        time.sleep(0.005)


def rpc(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    if msg.get("id") is None:
        return None
    return json.loads(proc.stdout.readline())


def main():
    bridge_dir = tempfile.mkdtemp(prefix="reaper_ipc_")
    stop = threading.Event()
    threading.Thread(target=fake_bridge, args=(bridge_dir, stop), daemon=True).start()

    env = dict(
        os.environ,
        REAPER_MCP_IPC_DIR=bridge_dir,
        REAPER_MCP_TIMEOUT="5",
        REAPER_MCP_HEARTBEAT_STALE="0.2",
    )
    proc = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
        text=True, env=env, encoding="utf-8",
    )
    failures = []

    def check(label, cond):
        print(("  PASS" if cond else "  FAIL"), label)
        if not cond:
            failures.append(label)

    try:
        src = open(BRIDGE, encoding="utf-8").read()
        check("beats are project QN",
              "TimeMap2_QNToTime(0, b" in src
              and "TimeMap2_beatsToTime(0, b, 0)" not in src)
        check("heartbeat file", "heartbeat" in src)
        check("busy file while dispatch blocked", "busy" in src)
        check("MIDI undo uses OnStateChange_Item",
              "Undo_OnStateChange_Item" in src)
        check("APPDATA mailbox", "reaper-mcp" in src and "APPDATA" in src)
        from reaper_mcp import __version__
        check("bridge VERSION matches the package", 'local VERSION = "%s"' % __version__ in src)
        pyproject = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
        check("pyproject version matches the package", 'version = "%s"' % __version__ in pyproject)
        check("batch inherits the render timeout", check_batch_timeout())
        check("lua json encoder handles mixed utf-8",
              "utf8_len" in src and "is_cont" in src)
        check("python reads mailbox as utf-8 replace", check_invalid_utf8_response())

        r = rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        check("initialize", r["result"]["serverInfo"]["name"] == "reaper-mcp")
        check("instructions present", "beats" in r["result"].get("instructions", ""))
        check("instructions ask for detail on new projects",
              "detail=true" in r["result"].get("instructions", ""))
        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        r = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in r["result"]["tools"]}
        check("small tool surface", 8 <= len(names) <= 16)
        check("core tools", {"status", "track", "midi", "fx", "batch", "run_lua",
                             "reaper_call"} <= names)
        check("mix primitives", {"send", "item", "envelope"} <= names)
        check("no 100-tool dump", "setup_sidechain_compression" not in names)
        print("       tools:", sorted(names), "count=", len(names))

        status_schema = next(t["inputSchema"] for t in r["result"]["tools"]
                             if t["name"] == "status")
        check("status schema has detail",
              "detail" in status_schema.get("properties", {}))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "status", "arguments": {}}})
        body = json.loads(r["result"]["content"][0]["text"])
        check("status via fake bridge", body["tracks"][0]["name"] == "Drums")
        check("light status has no time_sig", "time_sig" not in body)

        r = rpc(proc, {"jsonrpc": "2.0", "id": 31, "method": "tools/call",
                       "params": {"name": "status", "arguments": {"detail": True}}})
        body = json.loads(r["result"]["content"][0]["text"])
        check("detailed status has time_sig",
              body.get("time_sig") == {"numerator": 4, "denominator": 4})
        check("detailed status has markers", "markers" in body)
        check("detailed status does not guess chords", "chords" not in body
              and "chord" not in body)

        r = rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "track",
                                  "arguments": {"action": "add", "name": "Bass"}}})
        body = json.loads(r["result"]["content"][0]["text"])
        check("grouped track add", body["name"] == "Bass")

        r = rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "reaper_call",
                                  "arguments": {"func": "CountTracks", "args": [0]}}})
        check("reaper_call", json.loads(r["result"]["content"][0]["text"]) == 3)

        r = rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "batch", "arguments": {
                           "calls": [
                               {"func": "track", "action": "add", "name": "Pad"},
                               {"func": "run_lua", "code": "return 1"},
                           ]}}})
        body = json.loads(r["result"]["content"][0]["text"])
        check("batch expands grouped tools",
              body[0]["echo"] == "add_track")
        check("batch keeps run_lua", body[1]["echo"] == "run_lua")

        r = rpc(proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "run_lua", "arguments": {"code": "slow"}}})
        check("busy bridge is not stale", not r["result"].get("isError"))

        r = rpc(proc, {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                       "params": {"name": "status", "arguments": {}}})
        check("status after slow request", "Drums" in r["result"]["content"][0]["text"])

        r = rpc(proc, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                       "params": {"name": "nope", "arguments": {}}})
        check("unknown tool errors", "error" in r)

        r = rpc(proc, {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                       "params": {"name": "track", "arguments": {"action": "delete"}}})
        check("missing argument is named", r["result"].get("isError")
              and r["result"]["content"][0]["text"] == "missing argument: index")

        r = rpc(proc, {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                       "params": {"name": "batch", "arguments": {"calls": [
                           {"func": "track", "arguments": {"action": "delete"}},
                           {"func": "run_lua", "code": "return 1"}]}}})
        body = json.loads(r["result"]["content"][0]["text"])  # the fake bridge echoes funcs, it doesn't run Lua
        check("batch: a bad slot doesn't abort the rest", not r["result"].get("isError")
              and len(body) == 2 and body[0]["echo"] == "run_lua")

        rpc(proc, {"jsonrpc": "2.0", "method": "ping"})
        rpc(proc, {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "status", "arguments": {}}})
        r = rpc(proc, {"jsonrpc": "2.0", "id": 13, "method": "ping"})
        check("stdio: id-less messages get no reply line", r.get("id") == 13 and r.get("result") == {})
        for label, ok in check_http_front(env).items():
            check("http front: " + label, ok)

        proc.stdin.write("[1, 2]\n")
        proc.stdin.flush()
        bad = json.loads(proc.stdout.readline())
        r = rpc(proc, {"jsonrpc": "2.0", "id": 12, "method": "ping"})
        check("non-object input answered, server alive", bad["error"]["code"] == -32600 and r.get("result") == {})
    finally:
        stop.set()
        proc.kill()

    if failures:
        print("FAILED", failures)
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
