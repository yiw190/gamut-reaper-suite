#!/usr/bin/env python3
"""Minimal ACP agent over stdio for gateway tests: config options, streamed chunks, a tool call that asks for
permission, cancel, an auth-required mode (FAKE_ACP_AUTH=1 until `authenticate` is called), stray non-object
stdout lines (FAKE_ACP_NOISE=1) and a prompt containing "crash" that kills the process mid-turn."""

import json
import os
import sys
import threading

lock = threading.Lock()
need_auth = os.environ.get("FAKE_ACP_AUTH") == "1"
model = {"v": "fast"}
pending = {}
next_id = [1000]
cancelled = set()


def send(msg):
    with lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def config():
    return [{"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": model["v"],
             "options": [{"value": "fast", "name": "Fast"}, {"value": "smart", "name": "Smart"}]}]


def update(sid, u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


def ask(method, params):
    ev = threading.Event()
    with lock:
        next_id[0] += 1
        rid = next_id[0]
    pending[rid] = {"ev": ev}
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    ev.wait(30)
    return pending.pop(rid).get("result")


def prompt(rid, p):
    sid = p["sessionId"]
    text = p["prompt"][0]["text"]
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "[" + model["v"] + "] "}})
    if "crash" in text:
        os._exit(3)
    if "tool" in text:
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Edit REAPER project", "kind": "edit",
                     "status": "pending"})
        r = ask("session/request_permission", {"sessionId": sid, "toolCall": {"toolCallId": "t1", "title": "Edit REAPER project"},
                                               "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                                                           {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]})
        outcome = (r or {}).get("outcome", {})
        update(sid, {"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": "permission=" + str(outcome.get("optionId", outcome.get("outcome")))}})
    if sid in cancelled:
        cancelled.discard(sid)
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
        return
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " echo: " + text}})
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


for line in sys.stdin:
    msg = json.loads(line)
    if os.environ.get("FAKE_ACP_NOISE") == "1":
        with lock:
            sys.stdout.write("42\nnot json\n")
            sys.stdout.flush()
    if "method" not in msg:
        box = pending.get(msg.get("id"))
        if box:
            box["result"] = msg.get("result")
            box["ev"].set()
        continue
    m, p, rid = msg["method"], msg.get("params") or {}, msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": False},
              "authMethods": [{"id": "login", "name": "Log in", "description": "fake"}]}})
    elif m == "authenticate":
        need_auth = False
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif m == "session/new":
        if need_auth:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Authentication required"}})
        else:
            assert p["mcpServers"][0]["name"] == "reaper", p
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "s" + str(rid), "configOptions": config()}})
    elif m == "session/set_config_option":
        model["v"] = p["value"]
        send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": config()}})
    elif m == "session/prompt":
        threading.Thread(target=prompt, args=(rid, p), daemon=True).start()
    elif m == "session/cancel":
        cancelled.add(p["sessionId"])
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no " + m}})
