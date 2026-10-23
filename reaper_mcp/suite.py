"""hibiki: per-user install, health check, client registration and removal on macOS.

Everything runs from ROOT, the folder that holds reaper_mcp/, gateway/, panel/, reaper/ and scripts/ — the .pkg
payload or a git clone — with the Python that runs this module, so LaunchAgents point at whichever of the two ran
`setup`. Stdlib only (3.9+).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from reaper_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
PKG_ID = "io.github.yiw190.hibiki"
PKG_ROOT = Path("/Library/Application Support/Hibiki")
LABEL = "com.hibiki."
MCP_LABEL, GW_LABEL, WD_LABEL = LABEL + "reaper-mcp", LABEL + "agent-gateway", LABEL + "reaper-watchdog"
# LaunchAgents from before: hand-made ones, then the suite's own under its old name (gamut-reaper-suite, renamed
# Hibiki 2026-10-21); same jobs, replaced on setup
LEGACY_LABELS = {"com.yitian.reaper-mcp": MCP_LABEL, "com.yitian.reaper-watchdog": WD_LABEL,
                 "com.gamut-reaper-suite.reaper-mcp": MCP_LABEL, "com.gamut-reaper-suite.agent-gateway": GW_LABEL,
                 "com.gamut-reaper-suite.reaper-watchdog": WD_LABEL}
GW_PORT = 47892

WINDOWS = sys.platform == "win32"
IMGUI_VERSION = "0.10.0.5"
IMGUI_SHA256 = {  # by release file name
    "reaper_imgui-arm64.dylib": "1f90cf004c0bbada45358609c943f601b2cb0e625ecf7739ae5df8708ab705ae",
    "reaper_imgui-x86_64.dylib": "2209fec3eae03f22fe94b9d2681b04e21f41c7d4587cb0d69e4eb72e39a64e65",
    "reaper_imgui-x64.dll": "800b216e0937bf5bb6b08ba2767b7b974a746b3fc3b54943b4d3011da0a68f2a",
}
# `require 'imgui'` loads this shim from ImGui_GetBuiltinPath() (Scripts/ReaTeam Extensions/API). It isn't a release
# asset: ReaPack fetches it from the source tree, so without ReaPack the panel can't start unless setup installs it.
IMGUI_SHIM_URL = "https://raw.githubusercontent.com/cfillion/reaimgui/v%s/shims/imgui.lua" % IMGUI_VERSION
IMGUI_SHIM_SHA256 = "b73dac959d973e2d8fb47ea73588e84f81d4a3c7fb962c327d08427c2454b093"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"  # Windows: the supervisor starts at logon from here
RUN_VALUE = "Hibiki"
STARTUP_BEGIN = "-- >>> hibiki (managed by hibiki setup; edits inside are overwritten)"
STARTUP_END = "-- <<< hibiki"
LEGACY_STARTUP = (("-- >>> gamut-reaper-suite", "-- <<< gamut-reaper-suite"),)  # (begin prefix, end prefix)
# hand-copied hooks shipped before the managed block existed; setup swaps an exact copy for the managed block
LEGACY_HOOKS = ("""-- reaper-mcp: start the MCP bridge automatically every time REAPER launches (installed 2026-10-18).
local path = reaper.GetResourcePath() .. "/Scripts/reaper_mcp_bridge.lua"
local id = reaper.NamedCommandLookup("_RS70b9b9e08b3a1b47bef354e4c40290516371e9f9")
if id == 0 then id = reaper.AddRemoveReaScript(true, 0, path, true) end
if id ~= 0 then reaper.Main_OnCommand(id, 0) end

-- Gamut Chat panel: open on launch (2026-10-18). Toggle check so a second run never closes it.
do
  local gpath = reaper.GetResourcePath() .. "/Scripts/GamutChat/GamutChat.lua"
  local gid = reaper.NamedCommandLookup("_RS24f59c095963d6e7a1cf187e50fff047f87d7381")
  if gid == 0 then gid = reaper.AddRemoveReaScript(true, 0, gpath, true) end
  if gid ~= 0 and reaper.GetToggleCommandState(gid) ~= 1 then reaper.Main_OnCommand(gid, 0) end
end""",)
LEGACY_PANEL_DIRS = ("GamutChat",)

# exact versions; bump deliberately. Only minimax runs install scripts: better-sqlite3 fetches its native
# binding and @vscode/ripgrep fetches rg. kimi's postinstall only renames a legacy shim and node-pty ships
# darwin/win32 prebuilds; grok's launcher unpacks its binary on first run.
ACP_AGENTS = {
    "codex": {"package": "@agentclientprotocol/codex-acp", "version": "2.2.2", "bin": "codex-acp", "args": [],
              "name": "Codex", "description": "OpenAI Codex CLI over ACP"},
    "claude": {"package": "@agentclientprotocol/claude-agent-acp", "version": "0.94.0", "bin": "claude-agent-acp", "args": [],
               "name": "Claude Code", "description": "Claude Code (Claude Agent SDK) over ACP; uses the local claude login",
               "path_extra": ["~/.local/bin"]},
    "kimi": {"package": "@moonshot-ai/kimi-code", "version": "2.3.0", "bin": "kimi", "args": ["acp"],
             "name": "Kimi", "description": "Kimi Code CLI over ACP; uses the Kimi Code subscription login",
             "loginVariants": [
                 {"id": "global", "name": "Log in with kimi.ai", "description": "International account (kimi.ai)",
                  "args": ["--region", "global"]},
                 {"id": "cn", "name": "Log in with kimi.com", "description": "Mainland China account (kimi.com)",
                  "args": ["--region", "mainland-cn"]}]},
    # declares no ACP auth method; its login is `mcode acp login [--region global]`
    "minimax": {"package": "@minimax-ai/code", "version": "0.6.12", "bin": "mcode", "args": ["acp"],
                "allow_scripts": "@minimax-ai/code,better-sqlite3,@vscode/ripgrep",
                "name": "MiniMax", "description": "MiniMax Code CLI over ACP; uses your MiniMax account",
                "terminalLogin": {"id": "login", "name": "Log in", "args": ["login"]},
                "loginVariants": [
                    {"id": "global", "name": "Log in (global)", "description": "Global account (minimax.io)",
                     "args": ["--region", "global"]},
                    {"id": "cn", "name": "Log in (China)", "description": "Mainland China account (minimaxi.com)",
                     "args": []}]},
    # signs in through the browser itself (ACP auth method grok.com); needs SuperGrok / X Premium+ or XAI_API_KEY
    "grok": {"package": "@xai-official/grok", "version": "1.0.67", "bin": "grok",
             "args": ["--no-auto-update", "agent", "stdio"], "name": "Grok",
             "description": "xAI's Grok Build over ACP; SuperGrok or X Premium+ (beta)"},
    # Google login opens the browser itself; the API-key, Vertex and gateway methods need keys the panel can't set
    "gemini": {"package": "@google/gemini-cli", "version": "0.65.0", "bin": "gemini", "args": ["--acp"],
               "name": "Gemini", "description": "Gemini CLI over ACP; uses your Google AI Pro / Ultra login",
               "authMethods": ["oauth-personal"]},
    # ACP auth method copilot-login runs `copilot login` (device code) in a terminal
    "copilot": {"package": "@github/copilot", "version": "1.0.103", "bin": "copilot",
                "args": ["--acp", "--no-auto-update"], "name": "Copilot",
                "description": "GitHub Copilot CLI over ACP; uses your Copilot Pro / Pro+ subscription"},
}


class Paths:
    def __init__(self, resource_path: str | None = None) -> None:
        self.home = Path.home()
        if WINDOWS:
            appdata = Path(os.environ.get("APPDATA") or self.home / "AppData/Roaming")
            self.local = Path(os.environ.get("LOCALAPPDATA") or self.home / "AppData/Local")
            default_res = appdata / "REAPER"
            self.logs = self.local / "Hibiki/logs"
            default_ipc = appdata / "reaper-mcp/ipc"  # what server.py and bridge.lua use when APPDATA is set
            self.claude_desktop = appdata / "Claude"
        else:
            self.local = self.home / "Library/Application Support"
            default_res = self.home / "Library/Application Support/REAPER"
            self.logs = self.home / "Library/Logs"
            default_ipc = self.home / "Library/Application Support/reaper-mcp/ipc"
            self.claude_desktop = self.home / "Library/Application Support/Claude"
        self.resource = Path(resource_path or os.environ.get("REAPER_RESOURCE_PATH") or default_res)
        self.scripts = self.resource / "Scripts"
        self.cfg = self.home / ".config/reaper-mcp"
        self.token = self.cfg / "token"
        self.gw_cfg = self.home / ".config/agent-gateway/config.json"
        self.la = self.home / "Library/LaunchAgents"
        self.ipc = Path(os.environ.get("REAPER_MCP_IPC_DIR") or default_ipc)
        self.node = self.home / ".local/node"
        self.imgui_shim = self.scripts / "ReaTeam Extensions/API/imgui.lua"

    def plist(self, label: str) -> Path:
        return self.la / (label + ".plist")


REAPER_DOWNLOAD = "https://www.reaper.fm/download.php"


def reaper_app(p: Paths) -> Path | None:
    """macOS: REAPER.app (REAPER64.app in older releases) in /Applications or ~/Applications.
    Windows: reaper.exe under Program Files (or %LOCALAPPDATA%\\Programs), else where the registry says."""
    override = os.environ.get("HIBIKI_SYSTEM_APPLICATIONS")  # tests point it elsewhere
    if WINDOWS:
        roots = [override] if override else [os.environ.get(k) for k in ("ProgramW6432", "ProgramFiles",
                                                                         "ProgramFiles(x86)")] + [str(p.local / "Programs")]
        for r in filter(None, roots):
            for d in ("REAPER (x64)", "REAPER", "REAPER (ARM64)"):
                if (Path(r) / d / "reaper.exe").exists():
                    return Path(r) / d / "reaper.exe"
        if override:
            return None
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\REAPER", 0, winreg.KEY_READ | view) as k:
                        exe = Path(winreg.QueryValueEx(k, "")[0]) / "reaper.exe"
                except OSError:
                    continue
                if exe.exists():
                    return exe
        return None
    system = Path(override or "/Applications")
    for base in (system, p.home / "Applications"):
        for name in ("REAPER.app", "REAPER64.app"):
            if (base / name).exists():
                return base / name
    return None


def say(msg: str) -> None:
    if sys.stdout:  # None under pythonw (the Windows supervisor)
        print(msg, flush=True)


def no_services() -> bool:
    """Tests: write files only; no launchd, no Windows Run key, no background processes."""
    return bool(os.environ.get("HIBIKI_NO_SERVICES"))


def no_launchd() -> bool:
    return no_services() or sys.platform != "darwin"


def port() -> int:
    return int(os.environ.get("REAPER_MCP_PORT", "8787"))


def gamut_url() -> str:
    return os.environ.get("GAMUT_URL", "http://127.0.0.1:47891").rstrip("/")


def mcp_url() -> str:
    return "http://localhost:%d/mcp" % port()


def stdio_entry() -> dict:
    return {"command": sys.executable, "args": [str(ROOT / "reaper_mcp" / "server.py")]}


def need(path: Path) -> Path:
    if not path.exists():
        raise SystemExit("%s is missing: setup needs the whole suite (the .pkg or a git clone), not just the wheel" % path)
    return path


# ---------- launchd ----------

def launchctl(*args: str) -> int:
    if no_launchd():
        return 0
    return subprocess.run(["launchctl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def domain() -> str:
    return "gui/%d" % os.getuid()


def loaded(label: str) -> bool:
    if no_launchd():
        return False
    return launchctl("print", "%s/%s" % (domain(), label)) == 0


def stop_agent(p: Paths, label: str, delete: bool = False) -> bool:
    plist = p.plist(label)
    existed = plist.exists()
    if launchctl("bootout", "%s/%s" % (domain(), label)) == 0:
        deadline = time.time() + 15  # bootout returns before a job that shuts down gracefully is gone
        while loaded(label) and time.time() < deadline:
            time.sleep(0.2)
    if delete and existed:
        plist.unlink()
    return existed


def start_agent(p: Paths, label: str, args: list, env: dict | None = None, **extra) -> None:
    p.la.mkdir(parents=True, exist_ok=True)
    p.logs.mkdir(parents=True, exist_ok=True)
    log = str(p.logs / (label.split(".")[-1] + ".log"))
    body = {"Label": label, "ProgramArguments": args, "RunAtLoad": True,
            "StandardOutPath": log, "StandardErrorPath": log}
    if env:
        body["EnvironmentVariables"] = env
    body.update(extra)
    stop_agent(p, label)
    with open(p.plist(label), "wb") as f:
        plistlib.dump(body, f)
    for attempt in range(5):
        if launchctl("bootstrap", domain(), str(p.plist(label))) == 0:
            return
        time.sleep(1 + attempt)
    say("  launchctl bootstrap %s failed; see %s" % (label, log))


def wait_http(url: str, seconds: float = 10) -> bool:
    deadline = time.time() + seconds
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except (OSError, urllib.error.URLError):
            if time.time() > deadline:
                return False
            time.sleep(0.5)


# ---------- REAPER side ----------

def copy_if_changed(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.read_bytes() == src.read_bytes():
        return "up to date"
    if dst.exists():
        shutil.copy2(dst, dst.with_name(dst.name + ".bak"))
    tmp = dst.with_name(dst.name + ".part")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return "installed"


def install_bridge(p: Paths) -> None:
    say("  bridge: " + copy_if_changed(need(ROOT / "reaper_mcp/bridge.lua"), p.scripts / "reaper_mcp_bridge.lua"))


def install_panel(p: Paths) -> None:
    say("  panel: " + copy_if_changed(need(ROOT / "panel/Hibiki.lua"), p.scripts / "Hibiki/Hibiki.lua"))
    for old in LEGACY_PANEL_DIRS:
        if (p.scripts / old).exists():
            shutil.rmtree(p.scripts / old, ignore_errors=True)
            say("  removed the old panel folder Scripts/%s" % old)


def imgui_installed(p: Paths) -> bool:
    return any((p.resource / "UserPlugins").glob("reaper_imgui-*")) and p.imgui_shim.exists()


def fetch_checked(url: str, sha256: str, dst: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise SystemExit("download from %s failed its sha256 check" % url)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.with_name(dst.name + ".part").write_bytes(data)
    os.replace(dst.with_name(dst.name + ".part"), dst)


def install_imgui(p: Paths) -> None:
    if any((p.resource / "UserPlugins").glob("reaper_imgui-*")):
        say("  ReaImGui: already installed")
    else:
        if WINDOWS:
            if platform.machine().lower() in ("arm64", "aarch64"):
                raise SystemExit("Windows on ARM: install ReaImGui with ReaPack, then run setup again")
            name = "reaper_imgui-x64.dll"
        else:
            name = "reaper_imgui-%s.dylib" % ("arm64" if platform.machine() == "arm64" else "x86_64")
        fetch_checked("https://github.com/cfillion/reaimgui/releases/download/v%s/%s" % (IMGUI_VERSION, name),
                      IMGUI_SHA256[name], p.resource / "UserPlugins" / name)
        say("  ReaImGui %s: installed" % IMGUI_VERSION)
    if not p.imgui_shim.exists():
        fetch_checked(IMGUI_SHIM_URL, IMGUI_SHIM_SHA256, p.imgui_shim)
        say("  ReaImGui Lua shim: installed (%s)" % p.imgui_shim)


def strip_managed(text: str) -> str:
    out, end = [], None
    for line in text.splitlines(keepends=True):
        if end is None:
            if line.startswith(STARTUP_BEGIN):
                end = STARTUP_END
            for b, e in LEGACY_STARTUP:
                if line.startswith(b):
                    end = e
        if end is None:
            out.append(line)
        elif line.startswith(end):
            end = None
    text = "".join(out)
    for old in LEGACY_HOOKS:
        text = text.replace(old, "")
    return text


def install_cli_shim(p: Paths) -> None:
    """Source checkouts get a ~/.local/bin/hibiki launcher (the pkg links /usr/local/bin/hibiki); the panel's Recover runs it."""
    if WINDOWS or PKG_ROOT in ROOT.parents or ROOT == PKG_ROOT:
        return
    shim = p.home / ".local/bin/hibiki"
    body = '#!/bin/sh\nexec %s -s -E %s "$@"\n' % (shlex.quote(sys.executable), shlex.quote(str(ROOT / "reaper_mcp/cli.py")))
    if shim.exists() and not shim.is_symlink() and shim.read_text() == body:
        return
    if shim.is_symlink():
        shim.unlink()
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(body)
    os.chmod(shim, 0o755)
    say("  cli: %s" % shim)


def install_startup(p: Paths) -> None:
    hook = need(ROOT / "reaper/__startup.lua").read_text()
    path = p.scripts / "__startup.lua"
    old = path.read_text() if path.exists() else ""
    rest = strip_managed(old)
    if hook.strip() and hook.strip() in rest:  # a hand-copied hook from before setup existed: adopt it
        rest = rest.replace(hook.strip(), "", 1)
    rest = re.sub(r"\n{3,}", "\n\n", rest).strip("\n")
    if "reaper_mcp_bridge.lua" in rest:
        # another hook already starts the bridge; a second start would make REAPER ask to stop the running script
        say("  startup hook: %s already starts the bridge; left as is" % path)
        return
    new = (rest + "\n\n" if rest else "") + STARTUP_BEGIN + "\n" + hook.rstrip("\n") + "\n" + STARTUP_END + "\n"
    if new == old:
        say("  startup hook: up to date")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if old:
        path.with_name("__startup.lua.bak").write_text(old)
    path.write_text(new)
    say("  startup hook: installed")


def remove_startup(p: Paths) -> None:
    path = p.scripts / "__startup.lua"
    if not path.exists():
        return
    rest = strip_managed(path.read_text()).strip("\n")
    if rest:
        path.write_text(rest + "\n")
    else:
        path.unlink()


# ---------- token, MCP, gateway ----------

MIN_TOKEN = 32


def ensure_token(p: Paths) -> str:
    p.cfg.mkdir(parents=True, exist_ok=True)
    os.chmod(p.cfg, 0o700)
    if p.token.exists():
        token = p.token.read_text().strip()
        if len(token) >= MIN_TOKEN:
            os.chmod(p.token, 0o600)
            return token
        if token:
            say("  the MCP bearer is shorter than %d characters; replacing it (clients get the new one)" % MIN_TOKEN)
    token = ""
    env = p.cfg / "env"  # written by scripts/macos-host-bootstrap.sh; clients already hold this bearer
    if env.exists():
        m = re.search(r"^REAPER_MCP_BEARER=(\S+)", env.read_text(), re.M)
        token = m.group(1) if m and len(m.group(1)) >= MIN_TOKEN else ""
    token = token or secrets.token_urlsafe(32)
    fd = os.open(str(p.token), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def migrate_legacy(p: Paths) -> set:
    """Stop hand-made LaunchAgents that the suite replaces; returns the suite labels they stood for."""
    found = set()
    for old, new in LEGACY_LABELS.items():
        if stop_agent(p, old, delete=True):
            say("  replaced LaunchAgent %s with %s" % (old, new))
            found.add(new)
    return found


def start_mcp(p: Paths) -> bool:
    start_agent(p, MCP_LABEL, [sys.executable, str(ROOT / "reaper_mcp/http_server.py")],
                {"REAPER_MCP_PORT": str(port())}, KeepAlive=True, WorkingDirectory=str(ROOT))
    if no_launchd():
        return True
    ok = wait_http("http://127.0.0.1:%d/healthz" % port())
    say("  MCP on 127.0.0.1:%d: %s" % (port(), "up" if ok else "NOT answering, see %s" % (p.logs / "reaper-mcp.log")))
    return ok


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def gateway_config(p: Paths) -> None:
    cfg = load_json(p.gw_cfg) or {"acp": []}
    servers = [s for s in cfg.get("mcpServers", []) if s.get("name") != "reaper"]
    cfg["mcpServers"] = [dict(name="reaper", **stdio_entry())] + servers
    write_json(p.gw_cfg, cfg)


def start_gateway(p: Paths) -> None:
    start_agent(p, GW_LABEL, [sys.executable, str(need(ROOT / "gateway/agent_gateway.py"))],
                KeepAlive=True, WorkingDirectory=str(ROOT))
    if no_launchd():
        return
    ok = wait_http("http://127.0.0.1:%d/health" % GW_PORT)
    say("  agent gateway on 127.0.0.1:%d: %s" % (GW_PORT, "up" if ok else "NOT answering, see %s" % (p.logs / "agent-gateway.log")))


def watchdog_on(p: Paths) -> None:
    script = need(ROOT / "scripts/reaper-watchdog.sh")
    start_agent(p, WD_LABEL, ["/bin/bash", str(script)], StartInterval=30, ProcessType="Background")
    say("  watchdog: on (relaunches REAPER after a crash, restarts a hung one while the Mac is idle)")


def watchdog_off(p: Paths) -> None:
    stop_agent(p, WD_LABEL, delete=True)
    say("  watchdog: off")


# ---------- Windows services ----------
# One supervisor (`hibiki run`, under pythonw: no console) keeps the MCP HTTP front and the gateway running and
# restarts them when they exit; HKCU ...\CurrentVersion\Run starts it at logon. No admin rights, no Task Scheduler.

def win_python(gui: bool = False) -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe" if gui else "python.exe")
    return str(cand) if cand.exists() else str(exe)


def supervisor_cmd() -> list:
    return [win_python(gui=True), "-s", "-E", str(ROOT / "reaper_mcp/cli.py"), "run", "--port", str(port())]


def win_pid_file(p: Paths) -> Path:
    return p.logs / "supervisor.pid"


def win_supervisor_pid(p: Paths) -> int:
    """PID of the running supervisor, or 0."""
    try:
        pid = int(win_pid_file(p).read_text().strip())
    except (OSError, ValueError):
        return 0
    out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout.lower()
    return pid if "python" in out else 0  # a reused PID belongs to someone else


def win_autostart() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_VALUE)[0]
    except OSError:
        return ""


def win_services_stop(p: Paths, forget: bool = False) -> bool:
    if no_services():
        return False
    pid = win_supervisor_pid(p)
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    try:
        win_pid_file(p).unlink()
    except OSError:
        pass
    if forget and win_autostart():
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
    return bool(pid)


def win_services_start(p: Paths, wait: bool = True) -> bool:
    p.logs.mkdir(parents=True, exist_ok=True)
    if no_services():
        return True
    import winreg
    cmd = supervisor_cmd()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
    win_services_stop(p)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    io = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT), close_fds=True)
    try:  # out of the installer's (or a CI step's) job object, so it outlives the process that ran setup
        subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **io)
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **io)
    if not wait:
        return True
    ok = wait_http("http://127.0.0.1:%d/healthz" % port(), 20)
    say("  MCP on 127.0.0.1:%d: %s" % (port(), "up" if ok else "NOT answering, see %s" % (p.logs / "reaper-mcp.log")))
    gw = wait_http("http://127.0.0.1:%d/health" % GW_PORT, 10)
    say("  agent gateway on 127.0.0.1:%d: %s" % (GW_PORT, "up" if gw else "NOT answering, see %s"
                                                % (p.logs / "agent-gateway.log")))
    say("  autostart: on (HKCU Run '%s')" % RUN_VALUE)
    return ok


def cmd_run(a) -> int:
    """The Windows supervisor: start both services, restart whichever exits. Runs under pythonw, so no prints."""
    p = Paths()
    p.logs.mkdir(parents=True, exist_ok=True)
    try:
        return supervise(p, a.port)
    except BaseException:  # pythonw has no stderr: leave the reason where doctor's hint points
        import traceback
        with open(p.logs / "supervisor.log", "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc())
        raise


def supervise(p: Paths, port_: int) -> int:
    win_pid_file(p).write_text(str(os.getpid()))
    py = win_python() if WINDOWS else sys.executable
    env = dict(os.environ, REAPER_MCP_PORT=str(port_))
    jobs = {"reaper-mcp": [py, str(ROOT / "reaper_mcp/http_server.py")],
            "agent-gateway": [py, str(ROOT / "gateway/agent_gateway.py")]}
    flags = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
    procs: dict = {}
    while True:
        for name, argv in jobs.items():
            pr = procs.get(name)
            if pr is None or pr.poll() is not None:
                with open(p.logs / (name + ".log"), "ab") as log:
                    if pr is not None:
                        log.write(b"--- exited with %d, restarting\n" % pr.returncode)
                        log.flush()
                    procs[name] = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                                   cwd=str(ROOT), env=env, creationflags=flags)
        time.sleep(2)


# ---------- MCP clients ----------

def gamut_call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(gamut_url() + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def connect_gamut(p: Paths, remove: bool = False) -> str:
    try:
        servers = gamut_call("GET", "/api/remote-mcps").get("servers", [])
        old = next((s for s in servers if s.get("url") == mcp_url()), None)
        if remove:
            if old:
                gamut_call("DELETE", "/api/remote-mcps/" + old["id"])
                return "removed"
            return "not registered"
        auth = {"authType": "bearer", "accessToken": ensure_token(p)}
        if old:
            gamut_call("PATCH", "/api/remote-mcps/" + old["id"], auth)
            return "updated '%s'" % old.get("name")
        gamut_call("POST", "/api/remote-mcps", dict(auth, name="REAPER", url=mcp_url()))
        return "added 'REAPER'"
    except urllib.error.HTTPError as e:
        return "Gamut refused (%s): %s" % (e.code, e.read(300).decode(errors="replace"))
    except (OSError, ValueError):
        return "Gamut isn't running at %s; run `hibiki connect gamut` once it is" % gamut_url()


# Gamut assigns remote MCPs per agent: registering REAPER isn't enough, each agent that should drive REAPER needs it
def gamut_reaper_id() -> str:
    """Gamut's id for this MCP (matched by URL), or ''."""
    servers = gamut_call("GET", "/api/remote-mcps").get("servers", [])
    return next((s.get("id", "") for s in servers if s.get("url") == mcp_url()), "")


def gamut_agents() -> list:
    agents = gamut_call("GET", "/api/agents")
    agents = agents.get("agents", []) if isinstance(agents, dict) else agents
    return [{"slug": a["slug"], "name": a.get("name") or a["slug"]} for a in agents if isinstance(a, dict) and a.get("slug")]


def gamut_reaper_enabled(slug: str) -> bool:
    mcps = gamut_call("GET", "/api/agents/%s/remote-mcps" % urllib.parse.quote(slug)).get("mcps", [])
    return any(m.get("url") == mcp_url() for m in mcps)


def gamut_enable(p: Paths, slug: str) -> str:
    """Register the MCP in Gamut if needed, then assign it to one agent. Returns a one-line status."""
    mcp_id = gamut_reaper_id()
    if not mcp_id:
        connect_gamut(p)
        mcp_id = gamut_reaper_id()
    if not mcp_id:
        return "Gamut didn't take the REAPER MCP registration"
    if gamut_reaper_enabled(slug):
        return "already enabled"
    gamut_call("POST", "/api/agents/%s/remote-mcps" % urllib.parse.quote(slug), {"mcpIds": [mcp_id]})
    return "enabled"


def pick_gamut_agents(p: Paths, a) -> None:
    """`connect gamut`: enable REAPER for --agent / --all-agents, or ask on a terminal."""
    try:
        agents = gamut_agents()
    except (OSError, ValueError):
        return
    chosen = agents if a.all_agents else [x for x in agents if x["slug"] in (a.agent or [])
                                         or x["name"] in (a.agent or [])]
    if not chosen and not a.agent and sys.stdin and sys.stdin.isatty() and agents:
        say("Gamut assigns MCPs per agent. Enable REAPER for:")
        for i, x in enumerate(agents, 1):
            say("  %d. %s%s" % (i, x["name"], "  (already)" if gamut_reaper_enabled(x["slug"]) else ""))
        ans = input("numbers, comma-separated; 'a' = all; Enter = none: ").strip().lower()
        if ans == "a":
            chosen = agents
        else:
            idx = [int(t) for t in re.findall(r"\d+", ans)]
            chosen = [agents[i - 1] for i in idx if 0 < i <= len(agents)]
    for x in chosen:
        say("  %s: %s" % (x["name"], gamut_enable(p, x["slug"])))
    if not chosen:
        say("  REAPER isn't enabled for any agent yet: `hibiki connect gamut --agent <name>`, "
            "or the Hibiki panel's Agent settings")


def json_client(path: Path, key: str, remove: bool) -> str:
    cfg = load_json(path) if path.exists() else {}
    servers = cfg.setdefault(key, {})
    if remove:
        if servers.pop("reaper", None) is None:
            return "not registered"
        write_json(path, cfg)
        return "removed"
    if servers.get("reaper") == stdio_entry():
        return "up to date"
    servers["reaper"] = stdio_entry()
    write_json(path, cfg)
    return "registered in %s" % path


def claude_bin(p: Paths) -> str | None:
    return shutil.which("claude") or next((str(c) for c in [p.home / ".local/bin/claude"] if c.exists()), None)


def connect_claude_code(p: Paths, remove: bool = False) -> str:
    claude = claude_bin(p)
    if not claude:
        return "claude CLI not found"
    subprocess.run([claude, "mcp", "remove", "--scope", "user", "reaper"], capture_output=True)
    if remove:
        return "removed"
    e = stdio_entry()
    r = subprocess.run([claude, "mcp", "add", "--scope", "user", "reaper", "--", e["command"], *e["args"]],
                       capture_output=True, text=True)
    return "registered (user scope)" if r.returncode == 0 else "claude mcp add failed: " + (r.stderr or r.stdout).strip()


CLIENTS = {
    "gamut": (lambda p: True, connect_gamut),
    "claude-desktop": (lambda p: p.claude_desktop.is_dir(),
                       lambda p, remove=False: json_client(
                           p.claude_desktop / "claude_desktop_config.json", "mcpServers", remove)),
    "cursor": (lambda p: (p.home / ".cursor").is_dir(),
               lambda p, remove=False: json_client(p.home / ".cursor/mcp.json", "mcpServers", remove)),
    "claude-code": (lambda p: claude_bin(p) is not None, connect_claude_code),
}


def connect_detected(p: Paths, remove: bool = False) -> None:
    for name, (present, fn) in CLIENTS.items():
        if present(p):
            say("  %s: %s" % (name, fn(p, remove=remove)))


# ---------- ACP agents for the gateway ----------

def ensure_node(p: Paths) -> Path:
    """Node in ~/.local/node; returns the directory holding node and npm (bin/ on macOS, the root on Windows)."""
    bin_dir = p.node if WINDOWS else p.node / "bin"
    if (bin_dir / ("node.exe" if WINDOWS else "node")).exists():
        return bin_dir
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=30) as r:
        version = next(rel["version"] for rel in json.load(r) if rel["lts"])
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    name = "node-%s-%s-%s" % (version, "win" if WINDOWS else "darwin", arch)
    ext = ".zip" if WINDOWS else ".tar.gz"
    base = "https://nodejs.org/dist/%s/" % version
    with urllib.request.urlopen(base + "SHASUMS256.txt", timeout=30) as r:
        sums = dict(reversed(line.split()) for line in r.read().decode().splitlines() if line.strip())
    say("  installing Node %s into %s" % (version, p.node))
    with urllib.request.urlopen(base + name + ext, timeout=300) as r:
        data = r.read()
    if hashlib.sha256(data).hexdigest() != sums.get(name + ext):
        raise SystemExit("Node download failed its sha256 check")
    with tempfile.TemporaryDirectory() as tmp:
        arc = Path(tmp) / ("node" + ext)
        arc.write_bytes(data)
        if WINDOWS:
            import zipfile
            with zipfile.ZipFile(arc) as z:
                z.extractall(tmp)
        else:
            with tarfile.open(arc) as t:
                if hasattr(tarfile, "data_filter"):
                    t.extractall(tmp, filter="data")
                else:
                    t.extractall(tmp)
        if p.node.exists():
            shutil.rmtree(p.node)
        p.node.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(Path(tmp) / name), str(p.node))
    return bin_dir


def agents_add(p: Paths, slugs: list, restart: bool = True) -> None:
    unknown = [s for s in slugs if s not in ACP_AGENTS]
    if unknown:
        raise SystemExit("unknown agent(s) %s; choose from %s" % (", ".join(unknown), ", ".join(ACP_AGENTS)))
    bin_dir = ensure_node(p)
    env = dict(os.environ, PATH=os.pathsep.join([str(bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]))
    system_dirs = ([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
                    os.environ.get("SystemRoot", r"C:\Windows")] if WINDOWS else ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    cfg = load_json(p.gw_cfg) or {"acp": [], "mcpServers": [dict(name="reaper", **stdio_entry())]}
    for slug in slugs:
        a = ACP_AGENTS[slug]
        # Windows: --prefix pins the global shims to the node folder (npm's default there is %APPDATA%/npm)
        cmd = [str(bin_dir / ("npm.cmd" if WINDOWS else "npm")), "install", "-g", "--no-fund", "--no-audit"]
        if WINDOWS:
            cmd.append("--prefix=" + str(p.node))
        if a.get("allow_scripts"):
            cmd.append("--allow-scripts=" + a["allow_scripts"])
        else:
            cmd.append("--ignore-scripts")
        spec = "%s@%s" % (a["package"], a["version"])
        say("  npm install %s" % spec)
        subprocess.run(cmd + [spec], env=env, check=True)
        path = os.pathsep.join([str(bin_dir)] + [str(Path(x).expanduser()) for x in a.get("path_extra", [])]
                               + system_dirs)
        entry = {"slug": slug, "name": a["name"], "description": a["description"],
                 "command": [str(bin_dir / (a["bin"] + (".cmd" if WINDOWS else "")))] + a["args"], "env": {"PATH": path},
                 "cwd": "~/Documents/REAPER Media"}
        for k in ("loginVariants", "terminalLogin", "authMethods"):
            if a.get(k):
                entry[k] = a[k]
        cfg["acp"] = [x for x in cfg.get("acp", []) if x.get("slug") != slug] + [entry]
    write_json(p.gw_cfg, cfg)
    if not restart:  # the gateway itself installing (panel button): it loads the new agents in place
        pass
    elif WINDOWS:
        if win_supervisor_pid(p):
            win_services_start(p)  # restarts the gateway with the new agents
    elif loaded(GW_LABEL):
        launchctl("kickstart", "-k", "%s/%s" % (domain(), GW_LABEL))
    say("  added %s; they show up in the panel's agent picker" % ", ".join(slugs))


# ---------- commands ----------

def cmd_setup(a) -> int:
    if sys.platform not in ("darwin", "win32") and not no_services():
        raise SystemExit("setup is for macOS and Windows; elsewhere use `reaper-mcp install-bridge` and a stdio MCP config")
    p = Paths(a.resource_path)
    if not reaper_app(p):  # onboarding order: REAPER first, then Hibiki (the installers check the same up front)
        raise SystemExit("REAPER isn't installed. Download it from %s, %s, then run `hibiki setup` again."
                         % (REAPER_DOWNLOAD, "run its installer" if WINDOWS else "drag it to Applications"))
    say("hibiki %s from %s (python %s)" % (__version__, ROOT, sys.executable))
    say("REAPER")
    install_bridge(p)
    install_panel(p)
    if not a.no_reaimgui:
        install_imgui(p)
    install_startup(p)
    install_cli_shim(p)
    say("Services")
    ensure_token(p)
    if WINDOWS:
        gateway_config(p)
        ok = win_services_start(p)
    else:
        legacy = migrate_legacy(p)
        ok = start_mcp(p)
        gateway_config(p)
        start_gateway(p)
        if WD_LABEL in legacy or p.plist(WD_LABEL).exists():
            watchdog_on(p)
    if not a.no_connect:
        say("MCP clients")
        connect_detected(p)
    say("")
    say("Done. Restart REAPER: the bridge and the Hibiki panel start with it. Check with `hibiki doctor`.")
    return 0 if ok else 1


def bridge_version(path: Path) -> str:
    m = re.search(r'^local VERSION = "([^"]+)"', path.read_text(), re.M) if path.exists() else None
    return m.group(1) if m else ""


def cmd_doctor(a) -> int:
    p = Paths(a.resource_path)
    rows = []

    def row(level: str, name: str, detail: str) -> None:
        rows.append({"level": level, "check": name, "detail": detail})

    row("ok", "suite", "%s at %s, python %s" % (__version__, ROOT, sys.executable))
    app = reaper_app(p)
    row("ok" if app else "FAIL", "REAPER", str(app) if app else "not installed - get it from " + REAPER_DOWNLOAD)
    row("ok" if p.resource.is_dir() else "FAIL", "resource path", str(p.resource))
    bv = bridge_version(p.scripts / "reaper_mcp_bridge.lua")
    row("ok" if bv == __version__ else "FAIL", "bridge script",
        "%s (suite %s)" % (bv or "missing", __version__) + ("" if bv == __version__ else " - run setup"))
    panel = p.scripts / "Hibiki/Hibiki.lua"
    shipped = ROOT / "panel/Hibiki.lua"
    same = panel.exists() and shipped.exists() and panel.read_bytes() == shipped.read_bytes()
    row("ok" if same else "warn", "chat panel", "%s%s" % (panel, "" if same else " (missing or differs from this suite)"))
    row("ok" if imgui_installed(p) else "warn", "ReaImGui", "installed" if imgui_installed(p)
        else "missing (binary or %s): the panel needs it - run setup" % p.imgui_shim)
    startup = p.scripts / "__startup.lua"
    hooked = startup.exists() and "reaper_mcp_bridge.lua" in startup.read_text()
    row("ok" if hooked else "FAIL", "startup hook", str(startup) if hooked else "REAPER won't start the bridge on launch")
    if not p.token.exists():
        row("FAIL", "token", "%s missing" % p.token)
    elif WINDOWS:  # NTFS ACLs, not mode bits: the token sits in the user's profile
        row("ok", "token", str(p.token))
    else:
        mode = p.token.stat().st_mode & 0o777
        row("ok" if mode == 0o600 else "warn", "token", "%s mode %o" % (p.token, mode))
    if WINDOWS:
        auto = win_autostart()
        row("ok" if str(ROOT) in auto else "FAIL", "autostart", "HKCU Run \"%s\"" % RUN_VALUE if str(ROOT) in auto
            else ("points at another install - run setup" if auto else "missing - run setup"))
        pid = win_supervisor_pid(p)
        row("ok" if pid else "FAIL", "supervisor", "pid %d" % pid if pid else
            "not running - see %s, then run setup" % (p.logs / "supervisor.log"))
        row("ok", "watchdog", "macOS only")
    else:
        for label in (MCP_LABEL, GW_LABEL, WD_LABEL):
            if label == WD_LABEL and not p.plist(label).exists():
                row("ok", label, "off (optional)")
                continue
            row("ok" if loaded(label) else "FAIL", label, "loaded" if loaded(label) else "not loaded")
        for old in LEGACY_LABELS:
            if p.plist(old).exists():
                row("warn", old, "old LaunchAgent still installed - run setup")
    up = wait_http("http://127.0.0.1:%d/healthz" % port(), 0)
    row("ok" if up else "FAIL", "MCP http", "127.0.0.1:%d %s" % (port(), "answers" if up else "no answer"))
    gw = wait_http("http://127.0.0.1:%d/health" % GW_PORT, 0)
    row("ok" if gw else "warn", "agent gateway", "127.0.0.1:%d %s" % (GW_PORT, "answers" if gw else "no answer"))
    hb = p.ipc / "heartbeat"
    age = time.time() - hb.stat().st_mtime if hb.exists() else None
    row("ok" if age is not None and age < 5 else "warn", "bridge heartbeat",
        "%.1fs old" % age if age is not None else "never seen: start REAPER")
    try:
        servers = gamut_call("GET", "/api/remote-mcps").get("servers", [])
        reg = next((s for s in servers if s.get("url") == mcp_url()), None)
        row("ok" if reg else "warn", "Gamut", "registered as '%s'" % reg.get("name") if reg else
            "running, MCP not registered - `hibiki connect gamut`")
    except (OSError, ValueError):
        row("warn", "Gamut", "not running at %s" % gamut_url())
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print("%-5s %-38s %s" % (r["level"], r["check"], r["detail"]))
    return 1 if any(r["level"] == "FAIL" for r in rows) else 0


def cmd_connect(a) -> int:
    p = Paths()
    names = list(CLIENTS) if a.client == "all" else [a.client]
    for name in names:
        present, fn = CLIENTS[name]
        if a.client == "all" and not present(p):
            continue
        say("%s: %s" % (name, fn(p, remove=a.remove)))
        if name == "gamut" and not a.remove:
            pick_gamut_agents(p, a)
    return 0


def cmd_watchdog(a) -> int:
    if WINDOWS:
        raise SystemExit("the watchdog is macOS only for now")
    p = Paths()
    watchdog_on(p) if a.state == "on" else watchdog_off(p)
    return 0


def cmd_agents(a) -> int:
    agents_add(Paths(), a.agents)
    return 0


def cmd_uninstall(a) -> int:
    p = Paths(a.resource_path)
    if WINDOWS:
        win_services_stop(p, forget=True)
        say("stopped the services and removed the autostart entry")
    else:
        for label in (MCP_LABEL, GW_LABEL, WD_LABEL, *LEGACY_LABELS):
            if stop_agent(p, label, delete=True):
                say("removed LaunchAgent " + label)
    remove_startup(p)
    for f in (p.scripts / "reaper_mcp_bridge.lua", p.scripts / "reaper_mcp_bridge.lua.bak",
              p.scripts / "reaper_mcp_bridge.lua.new"):
        if f.exists():
            f.unlink()
    for d in ("Hibiki",) + LEGACY_PANEL_DIRS:
        shutil.rmtree(p.scripts / d, ignore_errors=True)
    say("removed the bridge, the panel and the startup hook (ReaImGui stays: other scripts use it)")
    connect_detected(p, remove=True)
    if a.purge:
        for d in (p.cfg, p.gw_cfg.parent):
            shutil.rmtree(d, ignore_errors=True)
        say("removed %s and %s" % (p.cfg, p.gw_cfg.parent))
    if WINDOWS:
        if "Programs" in ROOT.parts and "Hibiki" in ROOT.parts:
            say("App files: remove Hibiki in Settings -> Apps (its uninstaller runs this command too)")
    elif PKG_ROOT in ROOT.parents or ROOT == PKG_ROOT:
        say("App files: sudo rm -rf '%s' /usr/local/bin/hibiki && sudo pkgutil --forget %s"
            % (PKG_ROOT, PKG_ID))
    return 0


# ---------- recover ----------

PANEL_STREAM_AGENTS = ("HibikiPanel", "GamutChatPanel")  # the panel's SSE curl user agent, now and before the rename
RECOVER_WAIT = 10.0


def service_pid(label: str) -> int:
    """PID launchd reports for a loaded job, or 0."""
    try:
        out = subprocess.run(["launchctl", "print", "%s/%s" % (domain(), label)], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r"^\s*pid = (\d+)", out, re.M)
    return int(m.group(1)) if m else 0


def ps_table() -> dict:
    """pid -> parent pid for every process."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) == 2 and f[0].isdigit() and f[1].isdigit():
            table[int(f[0])] = int(f[1])
    return table


def child_pids(pid: int, table: dict | None = None) -> list:
    """Every process below `pid` (the gateway's ACP agents and their MCP servers)."""
    kids: dict = {}
    for c, parent in (ps_table() if table is None else table).items():
        kids.setdefault(parent, []).append(c)
    found, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in found:
                found.append(c)
                todo.append(c)
    return found


def kill_pids(pids: list) -> int:
    import signal
    n = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            n += 1
        except OSError:
            pass
    return n


def restart_services(p: Paths) -> tuple:
    if WINDOWS:
        win_services_start(p, wait=False)  # taskkill /T on the supervisor takes the gateway's agents with it
        return True, "restarted the supervisor (MCP + gateway)"
    if no_launchd():
        return True, "no launchd here; nothing to restart"
    jobs = {MCP_LABEL: [sys.executable, str(ROOT / "reaper_mcp/http_server.py")],
            GW_LABEL: [sys.executable, str(ROOT / "gateway/agent_gateway.py")]}
    old, notes, ok = [], [], True
    for label, args in jobs.items():
        pid = service_pid(label)
        if pid:
            old += child_pids(pid)
        if p.plist(label).exists() and loaded(label):
            try:
                rc = subprocess.run(["launchctl", "kickstart", "-k", "%s/%s" % (domain(), label)],
                                    capture_output=True, timeout=10).returncode
            except (OSError, subprocess.SubprocessError):
                rc = -1
            notes.append("%s %s" % (label.split(".")[-1], "restarted" if rc == 0 else "restart failed (%s)" % rc))
            ok = ok and rc == 0
        else:
            env = {"REAPER_MCP_PORT": str(port())} if label == MCP_LABEL else None
            start_agent(p, label, args, env, KeepAlive=True, WorkingDirectory=str(ROOT))
            notes.append("%s started" % label.split(".")[-1])
    time.sleep(0.5)
    table = ps_table()
    current = set()
    for label in jobs:
        pid = service_pid(label)
        if pid:
            current.update([pid] + child_pids(pid, table))
    killed = kill_pids([pid for pid in old if pid in table and pid not in current])
    if killed:
        notes.append("stopped %d leftover agent/MCP process%s" % (killed, "" if killed == 1 else "es"))
    return ok, ", ".join(notes)


def kill_panel_streams() -> tuple:
    if WINDOWS:
        filt = " -or ".join("$_.CommandLine -like '*-A %s*'" % ua for ua in PANEL_STREAM_AGENTS)
        ps = ("$p = Get-CimInstance Win32_Process -Filter \"Name='curl.exe'\" | Where-Object { %s }; "
              "$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
              "@($p).Count" % filt)
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                                 text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            return False, "could not list curl processes: %s" % e
        n = int(out) if out.isdigit() else 0
    else:
        n = 0
        for ua in PANEL_STREAM_AGENTS:
            pattern = "curl -s -N -A %s" % ua
            try:
                found = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5).stdout.split()
                if found:
                    subprocess.run(["pkill", "-f", pattern], capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                found = []
            n += len(found)
    return True, "killed %d panel stream%s" % (n, "" if n == 1 else "s") if n else "none running"


def repair_files(p: Paths) -> tuple:
    fixed = []
    if not (p.scripts / "reaper_mcp_bridge.lua").exists():
        install_bridge(p)
        fixed.append("bridge script")
    if not (p.scripts / "Hibiki/Hibiki.lua").exists():
        install_panel(p)
        fixed.append("panel")
    if not p.token.exists() or len(p.token.read_text().strip()) < MIN_TOKEN:
        ensure_token(p)
        fixed.append("token")
    return True, "restored " + ", ".join(fixed) if fixed else "all present"


def cmd_recover(a) -> int:
    """The panel's Recover button: restart the services, drop stuck streams, report what answers. Never kills REAPER."""
    import contextlib
    import io
    p = Paths(getattr(a, "resource_path", None))
    steps = []

    def step(name: str, fn) -> None:
        try:
            ok, detail = fn()
        except BaseException as e:  # noqa: BLE001 - one failed step must not hide the others
            ok, detail = False, str(e) or repr(e)
        steps.append({"name": name, "ok": bool(ok), "detail": detail})

    def wait_services() -> tuple:
        deadline = time.time() + RECOVER_WAIT
        mcp = gw = False
        while True:
            mcp = mcp or wait_http("http://127.0.0.1:%d/healthz" % port(), 0)
            gw = gw or wait_http("http://127.0.0.1:%d/health" % GW_PORT, 0)
            if (mcp and gw) or time.time() > deadline:
                break
            time.sleep(0.5)
        steps.append({"name": "MCP http", "ok": mcp, "detail": "127.0.0.1:%d %s" % (port(), "answers" if mcp else
                      "no answer - see %s" % (p.logs / "reaper-mcp.log"))})
        return gw, "127.0.0.1:%d %s" % (GW_PORT, "answers" if gw else "no answer - see %s" % (p.logs / "agent-gateway.log"))

    def bridge() -> tuple:
        hb = p.ipc / "heartbeat"
        if not hb.exists():
            return False, "bridge never seen: start REAPER"
        age = time.time() - hb.stat().st_mtime
        if age < 5:
            return True, "bridge alive (heartbeat %.1fs old)" % age
        return False, ("bridge stale (heartbeat %.0fs old): REAPER busy or hung; the watchdog restarts a hung REAPER"
                       % age)

    sink = sys.stderr if (a.json and sys.stderr) else (io.StringIO() if a.json else sys.stdout)
    with contextlib.redirect_stdout(sink):
        step("files", lambda: repair_files(p))
        step("restart services", lambda: restart_services(p))
        step("panel streams", kill_panel_streams)
        step("agent gateway", wait_services)
        step("REAPER bridge", bridge)
    up = {s["name"]: s["ok"] for s in steps}
    ok = up.get("MCP http", False) and up.get("agent gateway", False)
    if a.json:
        print(json.dumps({"ok": ok, "steps": steps}))
    else:
        for s_ in steps:
            print("%-4s %-17s %s" % ("ok" if s_["ok"] else "FAIL", s_["name"], s_["detail"]))
        print("recovered" if ok else "not recovered: see the FAIL lines")
    return 0 if ok else 1


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hibiki", description="REAPER MCP, Hibiki panel and agent gateway.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("setup", help="install or upgrade everything for this user (safe to re-run)")
    s.add_argument("--resource-path", help="REAPER resource path (default: ~/Library/Application Support/REAPER, "
                                           "%%APPDATA%%\\REAPER on Windows)")
    s.add_argument("--no-reaimgui", action="store_true", help="don't download ReaImGui")
    s.add_argument("--no-connect", action="store_true", help="don't register the MCP in Gamut / Claude / Cursor")
    s.set_defaults(fn=cmd_setup)
    d = sub.add_parser("doctor", help="check every piece and say what's wrong")
    d.add_argument("--resource-path")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_doctor)
    c = sub.add_parser("connect", help="register the MCP in a client")
    c.add_argument("client", choices=["all", *CLIENTS])
    c.add_argument("--remove", action="store_true")
    c.add_argument("--agent", action="append", metavar="NAME|SLUG", help="gamut: enable REAPER for this agent")
    c.add_argument("--all-agents", action="store_true", help="gamut: enable REAPER for every agent")
    c.set_defaults(fn=cmd_connect)
    w = sub.add_parser("watchdog", help="relaunch REAPER after crashes (for an unattended Mac)")
    w.add_argument("state", choices=["on", "off"])
    w.set_defaults(fn=cmd_watchdog)
    g = sub.add_parser("agents", help="install ACP agents for the panel (needs Node; installed into ~/.local/node)")
    g.add_argument("action", choices=["add"])
    g.add_argument("agents", nargs="+", metavar="|".join(ACP_AGENTS))
    g.set_defaults(fn=cmd_agents)
    rc = sub.add_parser("recover", help="restart the MCP and gateway, drop stuck panel streams, report (REAPER untouched)")
    rc.add_argument("--resource-path")
    rc.add_argument("--json", action="store_true")
    rc.set_defaults(fn=cmd_recover)
    u = sub.add_parser("uninstall", help="remove this user's install")
    u.add_argument("--resource-path")
    u.add_argument("--purge", action="store_true", help="also delete tokens, gateway config and ACP transcripts")
    u.set_defaults(fn=cmd_uninstall)
    r = sub.add_parser("run", help=argparse.SUPPRESS)  # the Windows supervisor (started by setup and at logon)
    r.add_argument("--port", type=int, default=port())
    r.set_defaults(fn=cmd_run)
    return ap


def main(argv: list) -> int:
    a = parser().parse_args(argv)
    if not getattr(a, "fn", None):
        parser().print_help()
        return 2
    return a.fn(a)
