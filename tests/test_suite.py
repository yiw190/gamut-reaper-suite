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


class FakeGamut(BaseHTTPRequestHandler):
    servers: list = []

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
        self._reply({"servers": self.servers})

    def do_POST(self):
        b = self._body()
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


def run(home, *args, gamut=""):
    env = dict(os.environ, HOME=str(home), HIBIKI_NO_LAUNCHCTL="1", GAMUT_URL=gamut or "http://127.0.0.1:9",
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
              home / "Library/Application Support/Claude", home / ".cursor"):
        d.mkdir(parents=True)
    # a Mac set up by hand before the suite: hook copied verbatim, bearer in env, old LaunchAgents, user's own hook
    (scripts / "__startup.lua").write_text("-- my own thing\nreaper.ShowConsoleMsg('hi')\n\n" + STARTUP)
    (res / "UserPlugins/reaper_imgui-arm64.dylib").write_bytes(b"x")
    (home / ".config/reaper-mcp/env").write_text("REAPER_MCP_BEARER=oldbearer\nREAPER_MCP_PORT=8787\n")
    for old in ("com.yitian.reaper-mcp", "com.yitian.reaper-watchdog", "com.yitian.cloudflared-reaper"):
        with open(la / (old + ".plist"), "wb") as f:
            plistlib.dump({"Label": old, "ProgramArguments": ["/usr/bin/true"]}, f)
    (home / ".config/agent-gateway/config.json").write_text(json.dumps({
        "acp": [{"slug": "codex", "command": ["x"]}],
        "mcpServers": [{"name": "reaper", "command": "/usr/bin/python3", "args": ["/old/python/server.py"]},
                       {"name": "other", "command": "y"}]}))
    FakeGamut.servers = [{"id": "s0", "name": "reaper-mcp", "url": "http://localhost:8787/mcp",
                          "authType": "bearer", "accessToken": "oldbearer"}]

    rc, out = run(home, "setup", gamut=gamut)
    check("setup exits 0", rc == 0, out)
    bridge = (scripts / "reaper_mcp_bridge.lua").read_text()
    check("bridge installed", bridge == (ROOT / "reaper_mcp/bridge.lua").read_text())
    check("panel installed", (scripts / "Hibiki/Hibiki.lua").exists())
    check("ReaImGui already present: no download", "already installed" in out, out)
    hook = (scripts / "__startup.lua").read_text()
    check("startup: hand-copied hook adopted into the managed block, not duplicated",
          hook.count("reaper_mcp_bridge.lua") == 1 and "-- >>> hibiki" in hook, hook)
    check("startup: user's own lines kept", hook.startswith("-- my own thing\nreaper.ShowConsoleMsg('hi')\n"), hook)
    check("startup backup written", (scripts / "__startup.lua.bak").exists())
    tok = home / ".config/reaper-mcp/token"
    check("token reuses the bearer clients already hold", tok.read_text().strip() == "oldbearer")
    check("token is 0600", tok.stat().st_mode & 0o777 == 0o600)
    check("legacy MCP + watchdog LaunchAgents removed",
          not (la / "com.yitian.reaper-mcp.plist").exists() and not (la / "com.yitian.reaper-watchdog.plist").exists())
    check("cloudflared LaunchAgent untouched", (la / "com.yitian.cloudflared-reaper.plist").exists())
    mcp = plistlib.loads((la / "com.hibiki.reaper-mcp.plist").read_bytes())
    check("MCP LaunchAgent runs reaper_mcp/http_server.py with this python",
          mcp["ProgramArguments"] == [sys.executable, str(ROOT / "reaper_mcp/http_server.py")], str(mcp))
    check("no secret in the MCP plist", "oldbearer" not in (la / "com.hibiki.reaper-mcp.plist").read_text())
    check("gateway LaunchAgent written", (la / "com.hibiki.agent-gateway.plist").exists())
    check("watchdog kept on (it was on before)", (la / "com.hibiki.reaper-watchdog.plist").exists())
    gw = json.loads((home / ".config/agent-gateway/config.json").read_text())
    check("gateway config: reaper entry repointed, others kept",
          gw["mcpServers"][0]["args"] == [str(ROOT / "reaper_mcp/server.py")]
          and [s["name"] for s in gw["mcpServers"]] == ["reaper", "other"] and gw["acp"][0]["slug"] == "codex", str(gw))
    check("Gamut: existing registration updated, not duplicated",
          len(FakeGamut.servers) == 1 and FakeGamut.servers[0]["accessToken"] == "oldbearer", str(FakeGamut.servers))
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

    rc, out = run(home, "connect", "cursor", "--remove")
    check("connect --remove", rc == 0 and "reaper" not in json.loads((home / ".cursor/mcp.json").read_text())["mcpServers"], out)

    rc, out = run(home, "uninstall", gamut=gamut)
    hook3 = (scripts / "__startup.lua").read_text()
    check("uninstall: services, scripts, managed hook gone; user lines stay",
          rc == 0 and not list(la.glob("com.hibiki.*")) and not (scripts / "reaper_mcp_bridge.lua").exists()
          and not (scripts / "Hibiki").exists() and "reaper_mcp_bridge" not in hook3 and "my own thing" in hook3, out)
    check("uninstall: Gamut registration removed", FakeGamut.servers == [], str(FakeGamut.servers))
    check("uninstall keeps the token without --purge", tok.exists())

    fresh = Path(tempfile.mkdtemp(prefix="grs_fresh_"))
    (fresh / "Library/Application Support/REAPER/UserPlugins").mkdir(parents=True)
    (fresh / "Library/Application Support/REAPER/UserPlugins/reaper_imgui-x86_64.dylib").write_bytes(b"x")
    rc, out = run(fresh, "setup", "--no-connect")
    t = (fresh / ".config/reaper-mcp/token").read_text().strip()
    check("fresh Mac: new random token, hook created", rc == 0 and len(t) >= 40
          and "-- >>> hibiki" in (fresh / "Library/Application Support/REAPER/Scripts/__startup.lua").read_text(), out)
    check("fresh Mac: watchdog stays off", not (fresh / "Library/LaunchAgents/com.hibiki.reaper-watchdog.plist").exists())

    # the Mac before the Hibiki rename: the suite's own LaunchAgents under its old name, the hand-copied v0.3 hook
    # (it launches Scripts/GamutChat/GamutChat.lua), the old panel folder
    old = Path(tempfile.mkdtemp(prefix="grs_renamed_"))
    ores = old / "Library/Application Support/REAPER"
    ola = old / "Library/LaunchAgents"
    for d in (ores / "Scripts/GamutChat", ores / "UserPlugins", ola):
        d.mkdir(parents=True)
    (ores / "UserPlugins/reaper_imgui-arm64.dylib").write_bytes(b"x")
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
          and "Scripts/Hibiki/Hibiki.lua" in hook, hook)
    check("rename: old panel folder gone, new one installed",
          not (ores / "Scripts/GamutChat").exists() and (ores / "Scripts/Hibiki/Hibiki.lua").exists(), out)

    httpd.shutdown()
    if failures:
        print("FAILED", failures)
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
