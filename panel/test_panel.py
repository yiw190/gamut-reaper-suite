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
EXT_STATE, CMDS, UI, RET = {}, {}, {}, {}
RECORD = true
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
  DeleteExtState = function(sec, key) EXT_STATE[sec .. '/' .. key] = nil end,
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
    v = function(...)
      if RECORD then UI[#UI + 1] = { name = k, args = table.pack(...) } end
      local r = RET[k]
      if r then return r(...) end
    end
  end
  rawset(t, k, v)
  return v
end })
package.preload.imgui = function() return function() return ImGui end end
-- fonts are distinct objects; text is 7 px per character
RET.CreateFont = function(name, flags) return { font = name, flags = flags } end
RET.CalcTextSize = function(_, s) return 7 * (utf8.len(s) or #s), 14 end
RET.GetFontSize = function() return 14 end

-- a log child 400 px wide: items advance the cursor by their height + 6 px item spacing, like ImGui
function LAYOUT(width, height)
  local L = { y = 0, scroll = 0, w = width or 400, h = height or 600 }
  local function adv(h) L.y = L.y + h + 6 end
  RET.GetCursorPosY = function() return L.y end
  RET.GetCursorScreenPos = function() return 0, L.y - L.scroll end
  RET.GetContentRegionAvail = function() return L.w, L.h end
  RET.GetScrollY = function() return L.scroll end
  RET.GetWindowHeight = function() return L.h end
  RET.GetWindowPos = function() return 0, 0 end
  RET.GetWindowSize = function() return L.w, L.h end
  RET.GetTextLineHeight = function() return 14 end
  RET.GetMousePos = function() return -1, -1 end
  RET.GetStyleVar = function() return 8, 6 end
  RET.Dummy = function(_, w, h) adv(h) end
  RET.InvisibleButton = function(_, id, w, h) adv(h) end
  RET.Spacing = function() adv(0) end
  RET.Text = function() adv(14) end
  RET.TextColored = RET.Text
  RET.TextDisabled = RET.Text
  return L
end

function OPS_EQUAL(a, b)
  if #a ~= #b then return false, 'op count ' .. #a .. ' vs ' .. #b end
  for i = 1, #a do
    local x, y = a[i], b[i]
    for k = 1, 7 do if x[k] ~= y[k] then return false, ('op %d field %d: %s vs %s'):format(i, k, tostring(x[k]), tostring(y[k])) end end
    if (x.nl or false) ~= (y.nl or false) then return false, 'op ' .. i .. ' nl' end
  end
  return true, ''
end
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

    def __init__(self, ext=None, token=None, env=None):
        import lupa

        self.tmp = Path(tempfile.mkdtemp())
        for k in ("TMPDIR", "TEMP", "TMP", "HOME", "USERPROFILE"):
            os.environ[k] = str(self.tmp)
        for k, v in (env or {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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


def test_one_click_install():
    p = Panel()
    p.boot([{"slug": "g1", "name": "Music", "backend": "gamut"}])
    p.requests()
    onb = p.m.ONB
    onb.install("kimi")
    (req,) = p.requests()
    check("install: POST /api/hibiki/install to the gateway", req.method == "POST" and req.url == GATEWAY + "/api/hibiki/install"
          and req.body == {"agent": "kimi"}, req)
    p.reply(req, 202, {"state": "installing"})
    (lst,) = p.requests()
    p.reply(lst, 200, [{"key": "kimi", "slug": "acp-kimi", "name": "Kimi", "description": "d", "installed": False,
                        "state": "installing", "error": ""}])
    check("install: progress tracked", onb.installing == "kimi")
    onb.load_installable()
    (lst,) = p.requests()
    p.reply(lst, 200, [{"key": "kimi", "slug": "acp-kimi", "name": "Kimi", "description": "d", "installed": True,
                        "state": "done", "error": ""}])
    (agents,) = p.requests()
    check("install done: agent list reloaded", agents.url == GATEWAY + "/api/agents")
    p.reply(agents, 200, [{"slug": "g1", "name": "Music", "backend": "gamut"}, {"slug": "acp-kimi", "name": "Kimi", "backend": "acp"}])
    onb.tick()
    check("install done: Kimi selected and settings opened for the login", p.S.agent == "acp-kimi" and p.S.cfgwin is True)
    urls = [r.url for r in p.requests()]
    check("install done: the new agent is started with ?probe=<slug> (probe=1 is only a cheap check)",
          GATEWAY + "/api/agents/acp-kimi/status?probe=acp-kimi" in urls
          and not any(u.endswith("/status?probe=1") for u in urls), urls)


def test_enable_reaper_in_gamut():
    p = Panel({"agent": "g1", "agent_name": "Music"})
    p.boot([{"slug": "g1", "name": "Music", "backend": "gamut"}])
    p.requests()
    onb = p.m.ONB
    check("reaper: unknown at first", onb.reaper_enabled("g1") is None)
    (req,) = [r for r in p.requests() if "gamut-reaper" in r.url]
    check("reaper: asks the gateway", req.method == "GET" and req.url.endswith("/api/hibiki/gamut-reaper?agent=g1"))
    p.reply(req, 200, {"registered": True, "enabled": False})
    check("reaper: disabled read back", onb.reaper_enabled("g1") is False)
    onb.enable_reaper("g1")
    (post,) = [r for r in p.requests() if r.method == "POST"]
    check("reaper: enable posts the agent", post.url.endswith("/api/hibiki/gamut-reaper") and post.body == {"agent": "g1"})
    p.reply(post, 200, {"status": "enabled"})
    (again,) = p.requests()
    p.reply(again, 200, {"registered": True, "enabled": True})
    check("reaper: enabled afterwards", onb.reaper_enabled("g1") is True)


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
    check("win: the stream kill is an encoded PowerShell script that spares streams started after it",
          "Stop-Process" in script and "-match 'HibikiPanel/" in script and "-gt 0" in script, script)
    check("win: every stream start has its own user agent",
          any(re.search(r" -s -N -A HibikiPanel/\d+\.1 ", c) for c in curl), curl)
    check("win: no write-out % on the cmd line (format read from a file)", not any("%{" in c for c in curl))


MD_TEXTS = [
    "# Title\n\nFirst paragraph with **bold**, *italic* and `code`.\n\nSecond one\nwith a hard break.\n\n## Sub\n\n- a\n- b\n  - nested\n1. one\n2. two\n\n> quote\n\n---\n\ntail",
    "Intro\n\n```lua\nlocal x = 1\n\nlocal y = 2\n\n\nprint(x)\n```\n\nafter the fence\n\n```\nunclosed\n\nstill code\n",
    "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\n| c |\n|:-|\n| x |\n\ntext\n\n# Heading after a blank\n\nend",
    "\n\nleading blanks\n\n\n\n### h3\n\n中文段落，没有空格也要按字换行。中文段落，没有空格也要按字换行。\n\n[link](http://x.y) words",
    "Windows\r\nline ends\r\n\r\n- item\r\n\r\n```\r\ncode\r\n\r\n```\r\n\r\nlast",
    "```\nfence at the start\n\n```\n|not a table\n\n| h |\n|--|\n| r |\n",
]


def test_md_incremental_matches_full():
    p = Panel()
    m, lua = p.m, p.lua
    eq = lua.globals().OPS_EQUAL
    bad = []
    for t_i, text in enumerate(MD_TEXTS):
        for width in (300, 120):
            L = None
            steps = sorted(set(list(range(1, len(text) + 1, 3)) + [len(text)]))
            for n in steps:
                t = text[:n]
                L = m.md_layout(L, t, width)
                ops, h = m.layout_md(t, width, 14)
                ok, why = eq(L.ops, ops)
                if not ok or L.h != h:
                    bad.append((t_i, width, n, why, L.h, h))
                    break
            fresh = m.md_layout(None, text, width)  # one-shot (completed message) path
            ok, why = eq(fresh.ops, m.layout_md(text, width, 14)[0])
            if not ok:
                bad.append((t_i, width, "fresh", why))
    check("H1: incremental layout = full layout at every streamed length (fences, tables, lists, CRLF)", not bad, bad[:3])
    L = None
    big = "".join("Paragraph %d with some words in it.\n\n" % i for i in range(400))
    for n in (len(big) // 2, len(big)):
        L = m.md_layout(L, big[:n], 300)
    check("H1: a long reply keeps its prefix (only the tail is laid out again)", L.cut > len(big) - 80, L.cut)


def _chat(p, n):
    msgs = []
    for i in range(n):
        msgs.append({"type": "user", "id": "u%d" % i, "content": {"text": "question %d" % i}})
        msgs.append({"type": "assistant", "id": "a%d" % i, "content": {"text": "answer %d\n\nwith **two** paragraphs" % i},
                     "toolCalls": [{"name": "mcp__reaper__status"}, {"name": "Bash"}] if i % 2 else []})
    return msgs


def test_offscreen_messages_are_dummies():
    p = Panel(SESSION)
    p.boot()
    p.sse({"type": "connected", "isActive": False})
    (req,) = find(p.requests(), "GET", "/sessions/s1/messages")
    p.reply(req, 200, _chat(p, 60))
    g, ui = p.lua.globals(), p.lua.globals().UI
    lay = g.LAYOUT(400, 600)
    p.m.draw_log()
    total = lay.y
    lay.y, lay.scroll = 0, total / 2
    g.UI = p.lua.table()
    p.m.draw_log()
    calls = [g.UI[i] for i in range(1, len(g.UI) + 1)]
    texts = [c for c in calls if c.name == "DrawList_AddTextEx"]
    ys = [c.args[5] for c in texts]
    check("H2: content height unchanged when off-screen messages are skipped", lay.y == total, (lay.y, total))
    check("H2: only messages near the visible window draw text",
          texts and all(-260 <= y <= 600 + 260 for y in ys), (len(texts), min(ys or [0]), max(ys or [0])))
    dummies = [c for c in calls if c.name == "Dummy"]
    check("H2: off-screen messages emit a Dummy and nothing else",
          len(dummies) > 100 and len(calls) < 6 * len(dummies) + 400, (len(dummies), len(calls)))
    lay.w, lay.y = 300, 0  # width change: every height is measured again
    p.m.draw_log()
    widths = {(m._h.w if m._h else None) for m in p.lua_list(p.S.msgs)}
    check("H2: a width change re-measures every message", widths == {300}, widths)
    h300 = lay.y
    lay.y = 0
    p.m.draw_log()
    check("H2: and then culls again with the new heights", lay.y == h300, (lay.y, h300))


def test_truncate_binary_search():
    p = Panel()
    tr = p.m.truncate
    check("truncate: fits unchanged", tr("short", 100) == "short")
    out = tr("abcdefghijklmnopqrstuvwxyz", 70)  # 7 px per char: 9 chars + "…" = 70
    check("truncate: longest prefix + ellipsis that fits", out == "abcdefghi…", out)
    out = tr("中文标题很长很长", 30)
    check("truncate: cuts on UTF-8 character boundaries", out == "中文标…", out)


def test_sse_backlog_crlf():
    p = Panel(SESSION)
    p.boot()
    p.sse({"type": "connected", "isActive": True})
    events = "".join("event: message\r\ndata: " + json.dumps({"type": "stream_delta", "text": "%d," % i}) + "\r\n\r\n"
                     for i in range(1000))
    cut = events.index("\r\n\r\n", len(events) // 3) + 1  # the chunk ends between \r and \n
    with open(p.S.stream.file, "ab") as f:
        f.write(events[:cut].encode())
    p.m.poll_stream()
    with open(p.S.stream.file, "ab") as f:
        f.write(events[cut:].encode())
    first = p.S.live[1].text.count(",") if len(p.S.live) else 0
    for _ in range(10):
        p.m.poll_stream()
    want = "".join("%d," % i for i in range(1000))
    check("M4: at most 400 events per frame", 0 < first <= 400, first)
    check("M4: a 1000-event CRLF backlog split mid-\\r\\n parses completely and in order",
          len(p.S.live) == 1 and p.S.live[1].text == want, p.S.live[1].text[-40:] if len(p.S.live) else None)


def test_incremental_transcript():
    p = Panel(SESSION)
    p.boot()
    p.sse({"type": "connected", "isActive": False})
    (req,) = find(p.requests(), "GET", "/sessions/s1/messages")
    check("M5: reconnect fetches the full transcript", "?" not in req.url, req.url)
    p.reply(req, 200, _chat(p, 2))
    first = p.S.msgs[1]
    first.marker = "kept"
    p.type_and_send("third question")
    p.sse({"type": "session_idle"})
    (req,) = find(p.requests(), "GET", "/messages?after=u1")
    check("M5: turn end asks only for messages after the one before the last reply", req is not None)
    p.reply(req, 200, {"after": "u1", "messages": [
        {"type": "assistant", "id": "a1", "content": {"text": "answer 1 (final)"}},
        {"type": "user", "id": "u2", "content": {"text": "third question"}},
        {"type": "assistant", "id": "a2", "content": {"text": "third answer"}}]})
    texts = [m.text for m in p.lua_list(p.S.msgs)]
    check("M5: merge keeps earlier messages (and their caches) and replaces the tail",
          texts == ["question 0", "answer 0\n\nwith **two** paragraphs", "question 1", "answer 1 (final)",
                    "third question", "third answer"] and p.S.msgs[1].marker == "kept", texts)
    p.sse({"type": "session_idle"})
    (req,) = find(p.requests(), "GET", "/messages?after=u2")
    p.reply(req, 200, _chat(p, 1))  # Gamut's own API: plain array = the whole transcript
    texts = [m.text for m in p.lua_list(p.S.msgs)]
    check("M5: an array answer replaces everything", len(texts) == 2 and p.S.msgs[1].marker is None, texts)
    p.sse({"type": "session_idle"})
    (req,) = find(p.requests(), "GET", "/messages?after=u0")
    p.reply(req, 200, {"full": True, "messages": _chat(p, 3)})
    check("M5: {full: true} replaces everything", len(p.S.msgs) == 6)
    p.sse({"type": "session_idle"})
    (req,) = find(p.requests(), "GET", "/messages?after=u2")
    p.S.msgs = p.lua.table()  # the anchor vanished locally (session switch race): fall back to a full fetch
    p.reply(req, 200, {"after": "u2", "messages": []})
    check("M5: unknown local anchor refetches in full", find(p.requests(), "GET", "/sessions/s1/messages"))


def test_private_dir():
    home = tempfile.mkdtemp()
    if OS_NAME.startswith("Win"):
        p = Panel(env={"TEMP": None, "TMP": None, "LOCALAPPDATA": home})
        check("#4 win: no TEMP -> %LOCALAPPDATA%\\Hibiki", p.m.DIR == home + "\\Hibiki\\", p.m.DIR)
        check("#4 win: q() closes % out of the quotes and refuses a double quote",
              p.m.q("a%PATH%b") == '"a"%"PATH"%"b"' and p.lua.eval("pcall")(p.m.q, 'x"y')[0] is False)
        return
    for tmp in (None, "/tmp", "/tmp/"):
        p = Panel(env={"TMPDIR": tmp})
        check("#4: TMPDIR=%r -> ~/Library/Caches/Hibiki, never /tmp" % tmp,
              p.m.DIR == str(p.tmp) + "/Library/Caches/Hibiki/", p.m.DIR)
    cmds = p.lua_list(p.lua.globals().CMDS)
    check("#4: the dir is created mode 700", any("mkdir -p -m 700" in c and "chmod 700" in c for c in cmds), cmds[:2])
    p = Panel(token="secret")
    cmds = p.lua_list(p.lua.globals().CMDS)
    i = next((n for n, c in enumerate(cmds) if "umask 077" in c and "chmod 600" in c and "gateway-auth" in c), None)
    j = next((n for n, c in enumerate(cmds) if "/usr/bin/curl" in c), None)
    check("#4: the auth header file is made 600 before the token is written", i is not None and j is not None and i < j)


def test_ids_validated():
    p = Panel({"agent": "acp-fake", "agent_name": "Fake", "session_acp-fake": "../../etc"})
    check("#9: a bad saved session id is dropped", p.S.sid == "")
    p.boot([{"slug": "ok-1", "name": "A", "backend": "acp"}, {"slug": "x;rm -rf ~", "name": "B", "backend": "acp"},
            {"slug": "..", "name": "C", "backend": "acp"}])
    check("#9: agents with unsafe slugs are ignored", [a.slug for a in p.lua_list(p.S.agents)] == ["ok-1"])
    p.S.agent, p.S.sid = "a b", "s1"  # set behind the validation: the URL still gets an encoded segment
    p.m.refresh_messages(True)
    cmds = [r.cmd.replace('"%"', "%") for r in p.requests()]
    check("#9: an unsafe slug never reaches a URL raw", cmds and all("a b" not in c for c in cmds)
          and any("/api/agents/a%20b/sessions/s1/messages" in c for c in cmds), cmds)


def _recover_job(p):
    cmds = p.lua_list(p.lua.globals().CMDS)
    job = [c for c in cmds if "recover --json" in c]
    out = re.search(r"""> ['"]([^'"]+recover[^'"]+)\.out['"]""", job[-1]).group(1) if job else None
    return job, out


def test_demo_typing():
    p = Panel(SESSION)
    p.boot()
    p.requests()
    text = "Make the pizz brighter, 中文 ok."
    p.lua.globals().EXT_STATE["Hibiki/demo_type"] = text
    t, partial, sent = 100.0, [], []
    for _ in range(400):
        p.m.demo_tick(t)
        partial.append(p.S.input)
        sent += [r for r in p.requests() if r.method == "POST" and "/messages" in r.url]
        if sent:
            break
        t += 0.05
    typed = [x for x in partial if x]
    check("demo: types the text gradually (valid UTF-8 prefixes) and then sends it once",
          len(set(typed)) > 10 and all(text.startswith(x) for x in typed) and len(sent) == 1
          and "Hibiki/demo_type" not in dict(p.lua.globals().EXT_STATE), (len(set(typed)), len(sent)))


def test_title_follows_gamut_rename():
    p = Panel({"agent": "ag1", "agent_name": "Music", "session_ag1": "s1", "title_ag1": "Make the pizz brighter"})
    p.boot([{"slug": "ag1", "name": "Music"}])
    p.requests()
    p.sse({"type": "session_updated"})
    p.m.frame()
    lists = [r for r in p.requests() if r.method == "GET" and r.url.endswith("/sessions")]
    if lists:
        p.reply(lists[-1], 200, [{"id": "s1", "name": "Brighter pizz in the kick sidechain"}, {"id": "s2", "name": "Other"}])
    check("title: Gamut's session_updated refetches the list and the header takes the new name (and persists it)",
          len(lists) == 1 and p.S.title == "Brighter pizz in the kick sidechain"
          and dict(p.lua.globals().EXT_STATE).get("Hibiki/title_ag1") == "Brighter pizz in the kick sidechain",
          (len(lists), p.S.title))


def test_recover():
    for case in ("ok", "bad", "missing"):
        p = Panel(SESSION)
        p.boot()
        p.sse({"type": "connected", "isActive": True}, {"type": "stream_delta", "text": "stuck"})
        p.requests()
        p.lua.execute("HIBIKI_TEST.sh('noop')")
        p.m.REC.start()
        job, base = _recover_job(p)
        reqs = p.requests()
        check("recover(%s): interrupts the running turn and starts the CLI in the background" % case,
              job and find(reqs, "POST", "/sessions/s1/interrupt") and p.m.REC.running is True
              and (job[-1].endswith("&") or "cmd.exe" in job[-1]), job)
        if not base:
            continue
        if case == "ok":
            body, code = json.dumps({"ok": True, "steps": [{"name": "gateway", "ok": True, "detail": "restarted"},
                                                          {"name": "bridge", "ok": False, "detail": "REAPER not running"}]}), 0
        elif case == "bad":
            body, code = "Traceback: boom", 1
        else:
            body, code = "", 127
        Path(base + ".out").write_text(body)
        Path(base + ".done").write_text(str(code))
        p.m.poll_http()
        lines = [l.text for l in p.lua_list(p.m.REC.lines)] if p.m.REC.lines else []
        if case == "ok":
            check("recover(ok): one line per step", lines == ["✓ gateway — restarted", "✗ bridge — REAPER not running"], lines)
        else:
            check("recover(%s): says to run hibiki recover in Terminal" % case,
                  len(lines) == 1 and "hibiki recover" in lines[0] and "Terminal" in lines[0], lines)
        check("recover(%s): not running any more" % case, p.m.REC.running is False)
        reqs = p.requests()
        (agents,) = [r for r in reqs if r.url.endswith("/api/agents")]
        p.reply(agents, 200, [{"slug": "acp-fake", "name": "Fake", "backend": "acp"}])
        reqs = p.requests()
        check("recover(%s): hosts re-resolved, chat reloaded in full and the stream restarted" % case,
              any(r.stream and r.url.endswith("/sessions/s1/stream") for r in reqs)
              and find(reqs, "GET", "/sessions/s1/messages"), reqs)


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
