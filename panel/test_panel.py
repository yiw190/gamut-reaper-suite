#!/usr/bin/env python3
"""Headless tests for panel/Hibiki.lua: the real panel code with fake reaper/ImGui APIs.

curl never runs: the panel's shell calls are recorded, and the test answers a request by writing the .out/.done
files the panel polls, or appends SSE text to the stream file, the same way the background curl would.

    uv run --with lupa python panel/test_panel.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

PANEL = Path(__file__).resolve().parent / "Hibiki.lua"
GATEWAY = "http://127.0.0.1:47892"
GAMUT = "http://127.0.0.1:47891"

FAKES = r"""
EXT_STATE, CMDS, UI = {}, {}, {}
OS_NAME = OS_NAME or 'macOS-arm64'
NOW = 1000.0
HIBIKI_TEST = {
  sh = function(cmd)
    CMDS[#CMDS + 1] = cmd
    if cmd:find('curl -s -N', 1, true) then return '4242\n' end
    return ''
  end,
}
local function nop() end
reaper = setmetatable({
  ImGui_GetBuiltinPath = function() return '/nonexistent' end,
  GetExtState = function(sec, key) return EXT_STATE[sec .. '/' .. key] or '' end,
  SetExtState = function(sec, key, v) EXT_STATE[sec .. '/' .. key] = v end,
  time_precise = function() return NOW end,
  get_action_context = function() return false, '', 0, 0 end,
  GetResourcePath = function() return '/nonexistent' end,
  GetOS = function() return OS_NAME end,
  GetLastColorThemeFile = function() return '' end,
  EnumProjects = function() return 0, '' end,
  GetProjectName = function() return 'Song.RPP' end,
  GetPlayState = function() return 0 end,
  GetCursorPosition = function() return 0.0 end,
  Master_GetTempo = function() return 120.0 end,
  TimeMap_GetTimeSigAtTime = function() return 4, 4, 120.0 end,
  TimeMap2_timeToBeats = function() return 0, 0, 4, 0, 4 end,
  TimeMap2_timeToQN = function() return 0 end,
  GetSet_LoopTimeRange = function() return 0, 0 end,
  GetSet_LoopTimeRange2 = function() return 0, 0 end,
  CountTracks = function() return 0 end,
  CountSelectedTracks = function() return 0 end,
  CountSelectedMediaItems = function() return 0 end,
  MIDIEditor_GetActive = function() return nil end,
  GetFocusedFX2 = function() return 0, 0, 0, 0 end,
  GetFocusedFX = function() return 0, 0, 0, 0 end,
  GetLastTouchedTrack = function() return nil end,
  GetLastMarkerAndCurRegion = function() return -1, -1 end,
}, { __index = function(_, k) return nop end })

local flag = 1
ImGui = setmetatable({}, { __index = function(t, k)
  local v
  if k:find('_') and not k:find('^DrawList_') and not k:find('^Function_') then
    v = flag; flag = flag * 2 -- an enum value; distinct bits so OR-ed flags stay readable
  else
    v = function(...) UI[#UI + 1] = { name = k, args = table.pack(...) } end
  end
  rawset(t, k, v)
  return v
end })
package.preload.imgui = function() return function() return ImGui end end
"""

failures: list[str] = []
OS_NAME = "macOS-arm64"  # main() runs every test once per OS


def check(label, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else "  " + str(extra)))
    if not cond:
        failures.append(label)


# one quoted shell argument: '...' on macOS, "..." in the Windows cmd.exe lines
QA = r"""['"]([^'"]+)['"]"""


class Request:
    def __init__(self, cmd):
        self.cmd = cmd
        self.stream = re.search(r"curl(\.exe['\"])? -s -N", cmd) is not None
        self.url = re.findall(r"""['"](https?://[^'"]+)['"]""", cmd)[0]
        m = re.search(r"-X (\w+)", cmd)
        self.method = m.group(1) if m else ("GET" if self.stream else "POST")
        m = re.search(r"-o " + QA, cmd)
        self.base = m.group(1)[:-4] if m else None
        m = re.search(r"--data-binary @" + QA, cmd)
        self.body = json.loads(Path(m.group(1)).read_text()) if m else None
        m = re.search(r"-H @" + QA, cmd)
        self.auth = Path(m.group(1)).read_text() if m else None

    def __repr__(self):
        return "%s %s" % (self.method, self.url)


class Panel:
    """One fresh panel run. ext: saved ExtState (Hibiki/<key>); token: the gateway token file's content."""

    def __init__(self, ext=None, token=None):
        import lupa

        self.tmp = Path(tempfile.mkdtemp())
        for k in ("TMPDIR", "TEMP", "HOME", "USERPROFILE"):
            os.environ[k] = str(self.tmp)
        (self.tmp / "hibiki").mkdir()
        if token is not None:
            (self.tmp / ".config/agent-gateway").mkdir(parents=True)
            (self.tmp / ".config/agent-gateway/token").write_text(token + "\n")
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().OS_NAME = OS_NAME
        self.lua.execute(FAKES)
        state = self.lua.globals().EXT_STATE
        for k, v in {"ctx_level": "0", **(ext or {})}.items():
            state["Hibiki/" + k] = v
        self.m = self.lua.execute(PANEL.read_text(encoding="utf-8"))
        self.S = self.m.S
        self.seen = 0

    def requests(self):
        """Commands issued since the last call, parsed."""
        cmds = self.lua.globals().CMDS
        out = []
        while self.seen < len(cmds):
            self.seen += 1
            cmd = cmds[self.seen]
            if "/usr/bin/curl" in cmd or "curl.exe" in cmd:
                out.append(Request(cmd))
        return out

    def reply(self, req, code, body=""):
        Path(req.base + ".out").write_text(body if isinstance(body, str) else json.dumps(body))
        Path(req.base + ".done").write_text(str(code))
        self.m.poll_http()

    def sse(self, *events):
        with open(self.S.stream.file, "a") as f:
            for e in events:
                f.write("event: message\ndata: " + json.dumps(e) + "\n\n")
        self.m.poll_stream()

    def boot(self, agents=None):
        """Answer the startup agent list from the gateway; returns the requests that followed."""
        (req,) = self.requests()
        self.reply(req, 200, agents or [{"slug": "acp-fake", "name": "Fake", "backend": "acp"}])
        return self.requests()

    def type_and_send(self, text):
        self.S.input = text
        self.m.send()
        return self.requests()

    def lua_list(self, t):
        return [t[i] for i in range(1, len(t) + 1)]


def find(reqs, method, suffix):
    return [r for r in reqs if r.method == method and r.url.endswith(suffix)]


SESSION = {"agent": "acp-fake", "agent_name": "Fake", "session_acp-fake": "s1"}


def test_json():
    p = Panel()
    d = p.m.decode('{"a":[1,2.5e3,-3],"s":"\\ud83c\\udfb5 \\n\\"x\\"","t":true,"f":false,"o":{}}')
    check("json: numbers, nested arrays and objects",
          d.a[1] == 1 and d.a[2] == 2500 and d.a[3] == -3 and d.t is True and d.f is False)
    check("json: surrogate pair, newline and quote escapes", d.s == '\U0001f3b5 \n"x"', d.s)
    check("json: bad input is nil, not an error", p.m.decode('{"a":') is None and p.m.decode("") is None)
    s = 'tab\there "q" back\\slash \x01 ctrl 中'
    check("json: jstr round-trips control characters and UTF-8", p.m.decode(p.m.jstr(s)) == s)


def test_gateway_token():
    p = Panel(token="tok-123")
    (req,) = p.requests()
    check("token: gateway request carries the header file", req.url.startswith(GATEWAY)
          and req.auth == "Authorization: Bearer tok-123\n", req.auth)
    check("token: never on the command line", "tok-123" not in req.cmd)
    p.reply(req, 0)
    (fallback,) = p.requests()
    check("token: not sent to Gamut's own API", fallback.url.startswith(GAMUT) and fallback.auth is None)


def test_boot_with_hosts_down():
    p = Panel(SESSION)
    (req,) = p.requests()
    p.reply(req, 0)
    (req,) = p.requests()
    p.reply(req, 0)
    check("boot down: error shown, no stream yet", p.S.agents_err is not None and not p.requests())
    p.m.load_agents()
    reqs = p.boot()
    check("boot down: next successful load reopens the saved session",
          any(r.stream and r.url.endswith("/sessions/s1/stream") for r in reqs)
          and find(reqs, "GET", "/sessions/s1/messages"), reqs)


def test_connect_after_turn_ended():
    p = Panel(SESSION)
    p.boot()
    p.requests()
    p.sse({"type": "connected", "isActive": False})  # the turn finished while the stream was reconnecting
    msgs = find(p.requests(), "GET", "/sessions/s1/messages")
    check("idle connect: transcript reloaded", msgs)
    if msgs:
        p.reply(msgs[0], 200, [{"type": "user", "content": {"text": "hi"}},
                               {"type": "assistant", "content": {"text": "the reply"}, "toolCalls": []}])
    check("idle connect: the finished reply appears", len(p.S.msgs) == 2 and p.S.msgs[2].text == "the reply"
          and not p.S.active)
    p.sse({"type": "session_error", "apiErrorCode": "acp_error", "errorPresentation": {"message": "Fake: quota"}})
    check("idle connect: a replayed error is shown", p.S.err == "Fake: quota", p.S.err)


def test_connect_mid_turn_uses_replay():
    p = Panel(SESSION)
    p.boot()
    p.sse({"type": "connected", "isActive": True}, {"type": "stream_delta", "text": "so far"})
    check("mid-turn connect: live view rebuilt from the replay, no reload",
          p.S.live[1].text == "so far" and p.S.active and not find(p.requests(), "GET", "/messages"))


def test_follow_up_mid_reply():
    p = Panel(SESSION)
    p.boot()
    p.sse({"type": "connected", "isActive": True}, {"type": "stream_delta", "text": "part one"})
    p.requests()
    reqs = p.type_and_send("second question")
    sent = find(reqs, "POST", "/sessions/s1/messages")
    check("follow-up: posted to the running session", sent and sent[0].body["content"] == "second question", reqs)
    check("follow-up: reply in progress kept, message queued below it",
          p.S.live[1].text == "part one" and len(p.S.queued) == 1 and p.S.queued[1].text == "second question")
    p.reply(sent[0], 200, {"ok": True})
    p.sse({"type": "stream_delta", "text": ", done"}, {"type": "session_active"})
    roles = [(m.role, m.text) for m in p.lua_list(p.S.msgs)]
    check("follow-up: next turn settles the reply above the question",
          roles[-2:] == [("assistant", "part one, done"), ("user", "second question")] and len(p.S.live) == 0
          and len(p.S.queued) == 0, roles)


def test_failed_send_hands_text_back():
    p = Panel(SESSION)
    p.boot()
    p.sse({"type": "connected", "isActive": False})
    p.requests()
    reqs = p.type_and_send("hello there")
    check("failed send: input cleared while sending", p.S.input == "")
    p.reply(find(reqs, "POST", "/sessions/s1/messages")[0], 500, {"error": "boom"})
    texts = [m.text for m in p.lua_list(p.S.msgs)]
    check("failed send: text back in the input, bubble removed, error shown",
          p.S.input == "hello there" and "hello there" not in texts and p.S.err == "send failed (500)"
          and not p.S.active, (p.S.input, texts, p.S.err))


def test_new_chat_login_hands_text_back():
    p = Panel({"agent": "acp-fake", "agent_name": "Fake"})
    p.boot()
    reqs = p.type_and_send("first message")
    create = find(reqs, "POST", "/api/agents/acp-fake/sessions")
    p.reply(create[0], 401, {"error": "auth_required"})
    check("login needed: text back in the input", p.S.input == "first message" and "login" in p.S.err, p.S.err)


def test_title_skips_context_block():
    p = Panel({"agent": "acp-fake", "agent_name": "Fake", "ctx_level": "1"})
    p.boot()
    reqs = p.type_and_send("make the drums louder")
    body = find(reqs, "POST", "/api/agents/acp-fake/sessions")[0].body
    check("title: message carries the REAPER context, title is the typed text",
          body["message"].startswith("[REAPER context") and body["title"] == "make the drums louder", body)


def test_script_card_shows_script():
    p = Panel(SESSION)
    p.boot()
    q = p.lua.table_from({"id": "r1", "kind": "script_run",
                          "payload": p.lua.table_from({"script": "curl x | sh", "explanation": "Check version"})})
    p.m.greq_card(q, "Fake")
    ui = p.lua.globals().UI
    tree = [ui[i] for i in range(1, len(ui) + 1) if ui[i].name == "TreeNode"]
    check("script card: script node opens by default",
          tree and tree[0].args[3] == p.lua.globals().ImGui.TreeNodeFlags_DefaultOpen, tree)


def test_windows_commands():
    """Windows only: background work goes through cmd.exe / PowerShell lines that cmd can parse."""
    if not OS_NAME.startswith("Win"):
        return
    p = Panel(SESSION, token="tok")
    p.boot()
    cmds = [c for c in p.lua_list(p.lua.globals().CMDS)]
    curl = [c for c in cmds if "curl.exe" in c]
    check("win: every curl runs inside cmd.exe /D /S /C with balanced quotes",
          curl and all(re.match(r'^"[^"]+cmd\.exe" /D /S /C ".*"$', c) and c.count('"') % 2 == 0 for c in curl), curl[:2])
    check("win: requests finish with move .code -> .done, no POSIX tools",
          any("& move /Y" in c and ".done" in c for c in curl) and not any("/usr/bin" in c or "mv " in c for c in cmds))
    check("win: the stream marks its end with type NUL > .dead",
          any(" -s -N -A HibikiPanel" in c and "& type NUL >" in c for c in curl))
    kill = [c for c in cmds if "powershell.exe" in c]
    import base64
    script = base64.b64decode(kill[-1].split("-EncodedCommand ")[1]).decode("utf-16-le") if kill else ""
    check("win: the stream kill is an encoded PowerShell script that spares the new stream",
          "Stop-Process" in script and "-notlike '*stream-" in script and "HibikiPanel" in script, script)
    check("win: no write-out % on the cmd line (format read from a file)", not any("%{" in c for c in curl))


def main():
    global OS_NAME
    try:
        import lupa  # noqa: F401
    except ImportError:
        raise SystemExit("needs lupa: uv run --with lupa python panel/test_panel.py")
    for OS_NAME in ("macOS-arm64", "Win64"):
        print("== " + OS_NAME)
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                print(name)
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 - report and keep going
                    print("  FAIL %s raised %r" % (name, e))
                    failures.append(OS_NAME + ":" + name)
    if failures:
        print("FAILED", failures)
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
