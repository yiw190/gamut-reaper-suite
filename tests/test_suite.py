#!/usr/bin/env python3
"""hibiki setup / doctor / connect / uninstall against a fake HOME, a fake Gamut and no launchd."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STARTUP = (ROOT / "reaper" / "__startup.lua").read_text()
OLD_BEARER = "old-bearer-0123456789abcdefghijklmnopqrstu"


class FakeGamut(BaseHTTPRequestHandler):
    servers: list = []
    assigned: dict = {}  # agent slug -> [mcp ids]

    def log_message(self, *a):
        pass

    def _reply(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def do_GET(self):
        if self.path == "/api/agents":
            return self._reply([{"slug": "ag1", "name": "Music"}, {"slug": "ag2", "name": "Other"}])
        if self.path.endswith("/remote-mcps") and self.path.startswith("/api/agents/"):
            slug = self.path.split("/")[3]
            ids = FakeGamut.assigned.get(slug, [])
            return self._reply({"mcps": [s for s in self.servers if s["id"] in ids]})
        self._reply({"servers": self.servers})

    def do_POST(self):
        b = self._body()
        if self.path.startswith("/api/agents/"):
            slug = self.path.split("/")[3]
            FakeGamut.assigned.setdefault(slug, []).extend(i for i in b["mcpIds"] if i not in FakeGamut.assigned.get(slug, []))
            return self._reply({"success": True})
        self.servers.append(dict(b, id="s%d" % len(self.servers)))
        self._reply({})

    def do_PATCH(self):
        b = self._body()
        sid = self.path.rsplit("/", 1)[-1]
        for s in self.servers:
            if s["id"] == sid:
                s.update(b)
        self._reply({})

    def do_DELETE(self):
        sid = self.path.rsplit("/", 1)[-1]
        FakeGamut.servers = [s for s in self.servers if s["id"] != sid]
        self._reply({})


def shim(res):
    """ReaImGui's imgui.lua shim, so setup doesn't download it (tests stay offline)."""
    f = res / "Scripts/ReaTeam Extensions/API/imgui.lua"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("-- shim")


def run(home, *args, gamut=""):
    env = dict(os.environ, HOME=str(home), HIBIKI_NO_SERVICES="1", GAMUT_URL=gamut or "http://127.0.0.1:9",
               USERPROFILE=str(home), APPDATA=str(home / "AppData/Roaming"), LOCALAPPDATA=str(home / "AppData/Local"),
               HIBIKI_SYSTEM_APPLICATIONS=str(home / "no-system-apps"),  # this Mac's own REAPER must not count
               PATH="/usr/bin:/bin")
    env.pop("REAPER_RESOURCE_PATH", None)
    r = subprocess.run([sys.executable, "-m", "reaper_mcp", *args], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


def main():
    failures = []

    def check(label, cond, detail=""):
        print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else "  " + detail[-800:]))
        if not cond:
            failures.append(label)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGamut)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    gamut = "http://127.0.0.1:%d" % httpd.server_address[1]

    home = Path(tempfile.mkdtemp(prefix="grs_home_"))
    res = home / "Library/Application Support/REAPER"
    scripts = res / "Scripts"
    la = home / "Library/LaunchAgents"
    for d in (scripts, res / "UserPlugins", la, home / ".config/reaper-mcp", home / ".config/agent-gateway",
              home / "Library/Application Support/Claude", home / ".cursor", home / "Applications/REAPER.app"):
        d.mkdir(parents=True)
    # a Mac set up by hand before the suite: hook copied verbatim, bearer in env, old LaunchAgents, user's own hook
    (scripts / "__startup.lua").write_text("-- my own thing\nreaper.ShowConsoleMsg('hi')\n\n" + STARTUP)
    (res / "UserPlugins/reaper_imgui-arm64.dylib").write_bytes(b"x")
    shim(res)
    (home / ".config/reaper-mcp/env").write_text("REAPER_MCP_BEARER=%s\nREAPER_MCP_PORT=8787\n" % OLD_BEARER)
    for old in ("com.yitian.reaper-mcp", "com.yitian.reaper-watchdog", "com.yitian.cloudflared-reaper"):
        with open(la / (old + ".plist"), "wb") as f:
            plistlib.dump({"Label": old, "ProgramArguments": ["/usr/bin/true"]}, f)
    (home / ".config/agent-gateway/config.json").write_text(json.dumps({
        "acp": [{"slug": "codex", "command": ["x"]}],
        "mcpServers": [{"name": "reaper", "command": "/usr/bin/python3", "args": ["/old/python/server.py"]},
                       {"name": "other", "command": "y"}]}))
    FakeGamut.servers = [{"id": "s0", "name": "reaper-mcp", "url": "http://localhost:8787/mcp",
                          "authType": "bearer", "accessToken": OLD_BEARER}]

    rc, out = run(home, "setup", gamut=gamut)
    check("setup exits 0", rc == 0, out)
    bridge = (scripts / "reaper_mcp_bridge.lua").read_text()
    check("bridge installed", bridge == (ROOT / "reaper_mcp/bridge.lua").read_text())
    check("panel installed", (scripts / "Hibiki/Hibiki.lua").exists())
    cli = home / ".local/bin/hibiki"
    check("source checkout: ~/.local/bin/hibiki launcher for the panel's Recover",
          cli.exists() and os.access(cli, os.X_OK) and str(ROOT / "reaper_mcp/cli.py") in cli.read_text(), out)
    check("ReaImGui already present: no download", "already installed" in out and "shim: installed" not in out, out)
    hook = (scripts / "__startup.lua").read_text()
    check("startup: hand-copied hook adopted into the managed block, not duplicated",
          hook.count("reaper_mcp_bridge.lua") == 1 and "-- >>> hibiki" in hook, hook)
    check("startup: user's own lines kept", hook.startswith("-- my own thing\nreaper.ShowConsoleMsg('hi')\n"), hook)
    check("startup backup written", (scripts / "__startup.lua.bak").exists())
    tok = home / ".config/reaper-mcp/token"
    check("token reuses the bearer clients already hold", tok.read_text().strip() == OLD_BEARER)
    check("token is 0600", tok.stat().st_mode & 0o777 == 0o600)
    check("legacy MCP + watchdog LaunchAgents removed",
          not (la / "com.yitian.reaper-mcp.plist").exists() and not (la / "com.yitian.reaper-watchdog.plist").exists())
    check("cloudflared LaunchAgent untouched", (la / "com.yitian.cloudflared-reaper.plist").exists())
    mcp = plistlib.loads((la / "com.hibiki.reaper-mcp.plist").read_bytes())
    check("MCP LaunchAgent runs reaper_mcp/http_server.py with this python",
          mcp["ProgramArguments"] == [sys.executable, str(ROOT / "reaper_mcp/http_server.py")], str(mcp))
    check("no secret in the MCP plist", OLD_BEARER not in (la / "com.hibiki.reaper-mcp.plist").read_text())
    check("gateway LaunchAgent written", (la / "com.hibiki.agent-gateway.plist").exists())
    check("watchdog kept on (it was on before)", (la / "com.hibiki.reaper-watchdog.plist").exists())
    gw = json.loads((home / ".config/agent-gateway/config.json").read_text())
    check("gateway config: reaper entry repointed, others kept",
          gw["mcpServers"][0]["args"] == [str(ROOT / "reaper_mcp/server.py")]
          and [s["name"] for s in gw["mcpServers"]] == ["reaper", "other"] and gw["acp"][0]["slug"] == "codex", str(gw))
    check("Gamut: existing registration updated, not duplicated",
          len(FakeGamut.servers) == 1 and FakeGamut.servers[0]["accessToken"] == OLD_BEARER, str(FakeGamut.servers))
    cd = json.loads((home / "Library/Application Support/Claude/claude_desktop_config.json").read_text())
    check("Claude Desktop registered (stdio)", cd["mcpServers"]["reaper"]["args"] == [str(ROOT / "reaper_mcp/server.py")])
    check("Cursor registered", "reaper" in json.loads((home / ".cursor/mcp.json").read_text())["mcpServers"])

    rc, out2 = run(home, "setup", gamut=gamut)
    hook2 = (scripts / "__startup.lua").read_text()
    check("second setup: idempotent", rc == 0 and hook2 == hook and "bridge: up to date" in out2
          and "startup hook: up to date" in out2 and len(FakeGamut.servers) == 1, out2)

    (scripts / "reaper_mcp_bridge.lua").write_text('local VERSION = "0.0.1"\n')
    rc, out = run(home, "doctor", "--json", gamut=gamut)
    rows = {r["check"]: r for r in json.loads(out[out.index("["):])} if "[" in out else {}
    check("doctor flags an outdated bridge", rc == 1 and rows.get("bridge script", {}).get("level") == "FAIL", out)
    check("doctor sees the Gamut registration", rows.get("Gamut", {}).get("level") == "ok", out)

    mhome = Path(tempfile.mkdtemp(prefix="grs_mcp_only_"))
    mres = mhome / "Library/Application Support/REAPER"
    for d in (mres / "Scripts", mhome / ".cursor", mhome / "Applications/REAPER.app"):
        d.mkdir(parents=True)
    FakeGamut.servers, before = [], list(FakeGamut.servers)
    rc, out = run(mhome, "setup", "--mcp-only", gamut=gamut)
    mhook = (mres / "Scripts/__startup.lua").read_text() if (mres / "Scripts/__startup.lua").exists() else ""
    check("setup --mcp-only: bridge + startup hook + Cursor, nothing else",
          rc == 0 and (mres / "Scripts/reaper_mcp_bridge.lua").exists() and "reaper_mcp_bridge.lua" in mhook
          and "reaper" in json.loads((mhome / ".cursor/mcp.json").read_text())["mcpServers"]
          and not (mres / "Scripts/Hibiki").exists() and not (mhome / "Library/LaunchAgents").exists()
          and not (mhome / ".config/reaper-mcp/token").exists() and not (mhome / ".config/agent-gateway").exists()
          and not FakeGamut.servers, out)
    FakeGamut.servers = before
    rc, out = run(mhome, "doctor", "--mcp-only", "--json", gamut=gamut)
    mrows = {r["check"]: r["level"] for r in json.loads(out[out.index("["):])} if "[" in out else {}
    check("doctor --mcp-only: passes without panel, token or services",
          rc == 0 and mrows.get("startup hook") == "ok" and "token" not in mrows and "MCP http" not in mrows, out)

    rc, out = run(home, "connect", "gamut", "--agent", "Music", gamut=gamut)
    check("connect gamut --agent: REAPER assigned to that agent only",
          rc == 0 and FakeGamut.assigned.get("ag1") == [FakeGamut.servers[0]["id"]] and "ag2" not in FakeGamut.assigned
          and "Music: enabled" in out, out)
    rc, out = run(home, "connect", "gamut", "--all-agents", gamut=gamut)
    check("connect gamut --all-agents: idempotent for the first, enables the rest",
          "Music: already enabled" in out and "Other: enabled" in out and FakeGamut.assigned["ag1"] == [FakeGamut.servers[0]["id"]], out)

    rc, out = run(home, "connect", "cursor", "--remove")
    check("connect --remove", rc == 0 and "reaper" not in json.loads((home / ".cursor/mcp.json").read_text())["mcpServers"], out)

    rc, out = run(home, "uninstall", gamut=gamut)
    hook3 = (scripts / "__startup.lua").read_text()
    check("uninstall: services, scripts, managed hook gone; user lines stay",
          rc == 0 and not list(la.glob("com.hibiki.*")) and not (scripts / "reaper_mcp_bridge.lua").exists()
          and not (scripts / "Hibiki").exists() and "reaper_mcp_bridge" not in hook3 and "my own thing" in hook3, out)
    check("uninstall: Gamut registration removed", FakeGamut.servers == [], str(FakeGamut.servers))
    check("uninstall keeps the token without --purge", tok.exists())

    # onboarding order: no REAPER yet -> setup refuses and touches nothing
    bare = Path(tempfile.mkdtemp(prefix="grs_noreaper_"))
    rc, out = run(bare, "setup", "--no-connect")
    check("no REAPER: setup refuses with the download link",
          rc != 0 and "reaper.fm" in out and not (bare / "Library").exists(), out)

    fresh = Path(tempfile.mkdtemp(prefix="grs_fresh_"))
    (fresh / "Library/Application Support/REAPER/UserPlugins").mkdir(parents=True)
    (fresh / "Applications/REAPER64.app").mkdir(parents=True)
    (fresh / "Library/Application Support/REAPER/UserPlugins/reaper_imgui-x86_64.dylib").write_bytes(b"x")
    shim(fresh / "Library/Application Support/REAPER")
    rc, out = run(fresh, "setup", "--no-connect")
    t = (fresh / ".config/reaper-mcp/token").read_text().strip()
    check("fresh Mac: new random token, hook created", rc == 0 and len(t) >= 40
          and "-- >>> hibiki" in (fresh / "Library/Application Support/REAPER/Scripts/__startup.lua").read_text(), out)
    (fresh / ".config/reaper-mcp/token").write_text("short\n")
    rc, out = run(fresh, "setup", "--no-connect")
    t2 = (fresh / ".config/reaper-mcp/token").read_text().strip()
    check("a bearer under 32 characters is replaced", rc == 0 and len(t2) >= 40 and t2 != "short", out)
    check("fresh Mac: watchdog stays off", not (fresh / "Library/LaunchAgents/com.hibiki.reaper-watchdog.plist").exists())

    # the Mac before the Hibiki rename: the suite's own LaunchAgents under its old name, the hand-copied v0.3 hook
    # (it launches Scripts/GamutChat/GamutChat.lua), the old panel folder
    old = Path(tempfile.mkdtemp(prefix="grs_renamed_"))
    ores = old / "Library/Application Support/REAPER"
    ola = old / "Library/LaunchAgents"
    for d in (ores / "Scripts/GamutChat", ores / "UserPlugins", ola, old / "Applications/REAPER.app"):
        d.mkdir(parents=True)
    (ores / "UserPlugins/reaper_imgui-arm64.dylib").write_bytes(b"x")
    shim(ores)
    (ores / "Scripts/GamutChat/GamutChat.lua").write_text("-- old panel")
    sys.path.insert(0, str(ROOT))
    from reaper_mcp import suite
    (ores / "Scripts/__startup.lua").write_text(suite.LEGACY_HOOKS[0] + "\n")
    for lab in ("com.gamut-reaper-suite.reaper-mcp", "com.gamut-reaper-suite.agent-gateway",
                "com.gamut-reaper-suite.reaper-watchdog"):
        with open(ola / (lab + ".plist"), "wb") as f:
            plistlib.dump({"Label": lab, "ProgramArguments": ["/usr/bin/true"]}, f)
    rc, out = run(old, "setup", "--no-connect")
    hook = (ores / "Scripts/__startup.lua").read_text()
    check("rename: old suite LaunchAgents replaced by com.hibiki.*",
          rc == 0 and not list(ola.glob("com.gamut-reaper-suite.*"))
          and {p_.name for p_ in ola.glob("com.hibiki.*")} == {"com.hibiki.reaper-mcp.plist", "com.hibiki.agent-gateway.plist",
                                                              "com.hibiki.reaper-watchdog.plist"}, out)
    check("rename: hand-copied hook swapped for the managed one",
          "GamutChat" not in hook and hook.count("reaper_mcp_bridge.lua") == 1 and "-- >>> hibiki" in hook
          and "\"Hibiki.lua\"" in hook, hook)
    check("rename: old panel folder gone, new one installed",
          not (ores / "Scripts/GamutChat").exists() and (ores / "Scripts/Hibiki/Hibiki.lua").exists(), out)

    import re
    check("ACP agents: every npm package pinned to an exact version",
          all(re.match(r"^\d+\.\d+\.\d+$", a.get("version", "")) for a in suite.ACP_AGENTS.values()), str(suite.ACP_AGENTS))
    calls = []
    real_run, real_node, real_write = suite.subprocess.run, suite.ensure_node, suite.write_json
    suite.subprocess.run = lambda cmd, **kw: calls.append(cmd)
    suite.ensure_node = lambda p_: Path("/nonexistent/node/bin")
    suite.write_json = lambda *a, **kw: None
    try:
        suite.agents_add(suite.Paths(), ["codex", "minimax"], restart=False)
    finally:
        suite.subprocess.run, suite.ensure_node, suite.write_json = real_run, real_node, real_write
    check("ACP agents: npm installs the pinned spec, scripts off unless the package needs them",
          len(calls) == 2 and calls[0][-1] == "@agentclientprotocol/codex-acp@2.2.2" and "--ignore-scripts" in calls[0]
          and calls[1][-1].startswith("@minimax-ai/code@") and "--ignore-scripts" not in calls[1], str(calls))

    check_recover(check, suite)
    check_stop_start(check, suite)

    httpd.shutdown()
    if failures:
        print("FAILED", failures)
        sys.exit(1)
    print("all passed")


def check_recover(check, suite):
    """`hibiki recover --json` with launchctl, ps, pkill and the HTTP checks faked."""
    import contextlib
    import io
    home = Path(tempfile.mkdtemp(prefix="grs_recover_"))
    res = home / "Library/Application Support/REAPER"
    (res / "Scripts").mkdir(parents=True)
    (home / "Library/LaunchAgents").mkdir(parents=True)
    for label in (suite.MCP_LABEL, suite.GW_LABEL):
        (home / "Library/LaunchAgents" / (label + ".plist")).write_bytes(b"x")
    ipc = home / "ipc"
    ipc.mkdir()
    (ipc / "heartbeat").write_text("1")
    os.utime(ipc / "heartbeat", (1, 1))
    calls, killed = [], []
    restarted = {"done": False}
    gw_pid = {suite.MCP_LABEL: "100", suite.GW_LABEL: "200"}

    class R:
        def __init__(self, out="", rc=0):
            self.stdout, self.returncode = out, rc

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["launchctl", "print"]:
            label = cmd[2].split("/")[-1]
            pid = gw_pid[label] if not restarted["done"] else str(int(gw_pid[label]) + 1000)
            return R("state = running\n\tpid = %s\n" % pid)
        if cmd[:3] == ["launchctl", "kickstart", "-k"]:
            if cmd[3].endswith(suite.GW_LABEL):
                restarted["done"] = True
            return R()
        if cmd[0] == "ps":
            if not restarted["done"]:
                return R(" 100 1\n 101 100\n 200 1\n 201 200\n 202 201\n")
            return R(" 1100 1\n 1101 1100\n 1200 1\n 201 1\n 202 201\n")
        if cmd[0] == "pgrep":
            return R("555\n" if "HibikiPanel" in cmd[-1] else "")
        return R()

    saved = {k: getattr(suite, k) for k in ("no_launchd", "domain", "wait_http", "kill_pids", "WINDOWS", "loaded")}
    real_run = suite.subprocess.run
    env_saved = {k: os.environ.get(k) for k in ("HOME", "REAPER_MCP_IPC_DIR")}
    os.environ["HOME"], os.environ["REAPER_MCP_IPC_DIR"] = str(home), str(ipc)
    suite.no_launchd, suite.domain, suite.WINDOWS = (lambda: False), (lambda: "gui/501"), False
    suite.loaded = lambda label: True
    suite.wait_http = lambda url, seconds=10: True
    suite.kill_pids = lambda pids: killed.extend(pids) or len(pids)
    suite.subprocess.run = fake_run
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = suite.main(["recover", "--json"])
    finally:
        suite.subprocess.run = real_run
        for k, v in saved.items():
            setattr(suite, k, v)
        for k, v in env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    try:
        r = json.loads(out.getvalue())
    except ValueError:
        r = {}
    steps = {x["name"]: x for x in r.get("steps", [])}
    check("recover --json: stdout is one JSON object with ok + steps", r.get("ok") is True and rc == 0
          and all(set(x) == {"name", "ok", "detail"} for x in r.get("steps", [])), out.getvalue())
    check("recover: kickstart -k for the MCP and the gateway",
          ["launchctl", "kickstart", "-k", "gui/501/" + suite.MCP_LABEL] in calls
          and ["launchctl", "kickstart", "-k", "gui/501/" + suite.GW_LABEL] in calls, str(calls))
    check("recover: the old gateway's orphaned agents are killed, the new ones kept",
          sorted(killed) == [201, 202], str(killed))
    check("recover: panel stream curls killed for both user agents",
          ["pkill", "-f", "curl -s -N -A HibikiPanel"] in calls and ["pgrep", "-f", "curl -s -N -A GamutChatPanel"] in calls
          and steps.get("panel streams", {}).get("detail") == "killed 1 panel stream", str(steps))
    check("recover: missing bridge / panel / token restored",
          (res / "Scripts/reaper_mcp_bridge.lua").exists() and (res / "Scripts/Hibiki/Hibiki.lua").exists()
          and (home / ".config/reaper-mcp/token").exists() and "bridge script" in steps.get("files", {}).get("detail", ""))
    check("recover: stale bridge reported, REAPER left alone",
          steps.get("REAPER bridge", {}).get("ok") is False and "stale" in steps["REAPER bridge"]["detail"]
          and not any("REAPER" in " ".join(c) and ("kill" in c[0]) for c in calls))
    check("recover: MCP + gateway up -> ok", steps.get("MCP http", {}).get("ok") and steps.get("agent gateway", {}).get("ok"))


def check_stop_start(check, suite):
    """`hibiki stop` / `start` with launchctl faked: services off and disabled across logins, then restored."""
    import contextlib
    import io
    home = Path(tempfile.mkdtemp(prefix="grs_stop_"))
    la = home / "Library/LaunchAgents"
    la.mkdir(parents=True)
    for label in (suite.MCP_LABEL, suite.GW_LABEL, suite.WD_LABEL):
        (la / (label + ".plist")).write_bytes(b"x")
    calls = []
    state = {suite.MCP_LABEL: True, suite.GW_LABEL: True, suite.WD_LABEL: False}

    def fake_launchctl(*args):
        calls.append(list(args))
        label = args[-1].split("/")[-1].replace(".plist", "")
        if args[0] == "bootout":
            state[label] = False
        if args[0] == "bootstrap":
            state[label] = True
        return 0

    saved = {k: getattr(suite, k) for k in ("no_launchd", "domain", "wait_http", "WINDOWS", "loaded", "launchctl",
                                             "service_pid", "kill_pids")}
    env_saved = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    suite.no_launchd, suite.domain, suite.WINDOWS = (lambda: False), (lambda: "gui/501"), False
    suite.launchctl = fake_launchctl
    suite.loaded = lambda label: state.get(label, False)
    suite.service_pid = lambda label: 0
    suite.kill_pids = lambda pids: 0
    suite.wait_http = lambda url, seconds=10: all(state[x] for x in (suite.MCP_LABEL, suite.GW_LABEL))
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc1 = suite.main(["stop"])
            marker = json.loads((home / ".config/reaper-mcp/stopped.json").read_text())
            after_stop = dict(state)
            with contextlib.redirect_stdout(io.StringIO()) as rec:
                rc_rec = suite.main(["recover", "--json"])
            calls_stop = list(calls)
            rc2 = suite.main(["start"])
    finally:
        for k, v in saved.items():
            setattr(suite, k, v)
        os.environ["HOME"] = env_saved
    check("stop: every installed service booted out and disabled for later logins",
          rc1 == 0 and not any(after_stop.values())
          and all(["disable", "gui/501/" + x] in calls_stop for x in (suite.MCP_LABEL, suite.GW_LABEL, suite.WD_LABEL)),
          str(calls_stop))
    check("stop: remembers what was running (watchdog was off)",
          sorted(marker.get("running", [])) == sorted([suite.MCP_LABEL, suite.GW_LABEL]), str(marker))
    r = json.loads(rec.getvalue() or "{}")
    check("recover after stop: refuses and points at `hibiki start`, restarts nothing",
          rc_rec == 1 and r.get("ok") is False and "hibiki start" in r["steps"][0]["detail"]
          and not any(c[0] in ("kickstart", "bootstrap") for c in calls_stop), rec.getvalue())
    check("start: enables and bootstraps only what was running, clears the marker",
          rc2 == 0 and state[suite.MCP_LABEL] and state[suite.GW_LABEL] and not state[suite.WD_LABEL]
          and ["enable", "gui/501/" + suite.MCP_LABEL] in calls
          and not (home / ".config/reaper-mcp/stopped.json").exists(), str(calls[len(calls_stop):]))


def main_windows():
    """The same flow on Windows paths: %APPDATA%\\REAPER, REAPER under Program Files, no LaunchAgents."""
    failures = []

    def check(label, cond, detail=""):
        print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else "  " + detail[-800:]))
        if not cond:
            failures.append(label)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGamut)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    gamut = "http://127.0.0.1:%d" % httpd.server_address[1]

    bare = Path(tempfile.mkdtemp(prefix="hb_noreaper_"))
    rc, out = run(bare, "setup", "--no-connect")
    check("win: no REAPER -> setup refuses with the download link", rc != 0 and "reaper.fm" in out, out)

    home = Path(tempfile.mkdtemp(prefix="hb_win_"))
    res = home / "AppData/Roaming/REAPER"
    scripts = res / "Scripts"
    (home / "no-system-apps/REAPER (x64)").mkdir(parents=True)
    (home / "no-system-apps/REAPER (x64)/reaper.exe").write_bytes(b"MZ")
    (res / "UserPlugins").mkdir(parents=True)
    (res / "UserPlugins/reaper_imgui-x64.dll").write_bytes(b"MZ")
    shim(res)
    (home / "AppData/Roaming/Claude").mkdir(parents=True)
    (home / ".cursor").mkdir()
    FakeGamut.servers = []
    rc, out = run(home, "setup", gamut=gamut)
    check("win: setup exits 0", rc == 0, out)
    check("win: bridge in %APPDATA%\\REAPER\\Scripts", (scripts / "reaper_mcp_bridge.lua").read_text()
          == (ROOT / "reaper_mcp/bridge.lua").read_text())
    check("win: panel installed", (scripts / "Hibiki/Hibiki.lua").exists())
    check("win: ReaImGui present, nothing downloaded", "already installed" in out and "shim: installed" not in out, out)
    hook = (scripts / "__startup.lua").read_text()
    check("win: startup hook written", "-- >>> hibiki" in hook and "Hibiki.lua" in hook, hook)
    check("win: token written", (home / ".config/reaper-mcp/token").read_text().strip() != "")
    gw = json.loads((home / ".config/agent-gateway/config.json").read_text())
    check("win: gateway config points at this server.py", gw["mcpServers"][0]["args"] == [str(ROOT / "reaper_mcp/server.py")])
    check("win: Gamut registration added", len(FakeGamut.servers) == 1 and FakeGamut.servers[0]["url"].endswith("/mcp"))
    cd = json.loads((home / "AppData/Roaming/Claude/claude_desktop_config.json").read_text())
    check("win: Claude Desktop registered under %APPDATA%\\Claude", "reaper" in cd["mcpServers"])
    check("win: no LaunchAgents", not (home / "Library").exists())
    rc, out2 = run(home, "setup", gamut=gamut)
    check("win: second setup idempotent", rc == 0 and "bridge: up to date" in out2 and len(FakeGamut.servers) == 1, out2)
    rc, out = run(home, "doctor", "--json", gamut=gamut)
    rows = {r["check"]: r for r in json.loads(out[out.index("["):])} if "[" in out else {}
    check("win: doctor finds REAPER, bridge, panel, ReaImGui", all(rows.get(k, {}).get("level") == "ok"
          for k in ("REAPER", "bridge script", "chat panel", "ReaImGui", "startup hook", "token")), out)
    rc, out = run(home, "uninstall", gamut=gamut)
    check("win: uninstall removes bridge, panel, hook block, registrations",
          rc == 0 and not (scripts / "reaper_mcp_bridge.lua").exists() and not (scripts / "Hibiki").exists()
          and FakeGamut.servers == [], out)

    httpd.shutdown()
    if failures:
        print("FAILED", failures)
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main_windows() if os.name == "nt" else main()
