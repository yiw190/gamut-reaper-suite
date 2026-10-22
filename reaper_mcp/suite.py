"""gamut-reaper-suite: per-user install, health check, client registration and removal on macOS.

Everything runs from ROOT, the folder that holds reaper_mcp/, gateway/, gamut-chat/, reaper/ and scripts/ — the .pkg
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
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from reaper_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
PKG_ID = "io.github.yiw190.gamut-reaper-suite"
PKG_ROOT = Path("/Library/Application Support/GamutReaperSuite")
LABEL = "com.gamut-reaper-suite."
MCP_LABEL, GW_LABEL, WD_LABEL = LABEL + "reaper-mcp", LABEL + "agent-gateway", LABEL + "reaper-watchdog"
# hand-made LaunchAgents from before the suite: same jobs, replaced on setup
LEGACY_LABELS = {"com.yitian.reaper-mcp": MCP_LABEL, "com.yitian.reaper-watchdog": WD_LABEL}
GW_PORT = 47892

IMGUI_VERSION = "0.10.0.5"
IMGUI_SHA256 = {
    "arm64": "1f90cf004c0bbada45358609c943f601b2cb0e625ecf7739ae5df8708ab705ae",
    "x86_64": "2209fec3eae03f22fe94b9d2681b04e21f41c7d4587cb0d69e4eb72e39a64e65",
}
STARTUP_BEGIN = "-- >>> gamut-reaper-suite (managed by gamut-reaper-suite setup; edits inside are overwritten)"
STARTUP_END = "-- <<< gamut-reaper-suite"
LEGACY_STARTUP_BEGIN = "-- >>> gamut-reaper-suite (managed by install.sh)"

ACP_AGENTS = {
    "codex": {"package": "@agentclientprotocol/codex-acp", "bin": "codex-acp", "args": [],
              "name": "Codex", "description": "OpenAI Codex CLI over ACP"},
    "claude": {"package": "@agentclientprotocol/claude-agent-acp", "bin": "claude-agent-acp", "args": [],
               "name": "Claude Code", "description": "Claude Code (Claude Agent SDK) over ACP; uses the local claude login",
               "path_extra": ["~/.local/bin"]},
    "kimi": {"package": "@moonshot-ai/kimi-code", "bin": "kimi", "args": ["acp"], "allow_scripts": "@moonshot-ai/kimi-code,node-pty",
             "name": "Kimi", "description": "Kimi Code CLI over ACP; uses the Kimi Code subscription login",
             "loginVariants": [
                 {"id": "global", "name": "Log in with kimi.ai", "description": "International account (kimi.ai)",
                  "args": ["--region", "global"]},
                 {"id": "cn", "name": "Log in with kimi.com", "description": "Mainland China account (kimi.com)",
                  "args": ["--region", "mainland-cn"]}]},
}


class Paths:
    def __init__(self, resource_path: str | None = None) -> None:
        self.home = Path.home()
        self.resource = Path(resource_path or os.environ.get("REAPER_RESOURCE_PATH")
                             or self.home / "Library/Application Support/REAPER")
        self.scripts = self.resource / "Scripts"
        self.cfg = self.home / ".config/reaper-mcp"
        self.token = self.cfg / "token"
        self.gw_cfg = self.home / ".config/agent-gateway/config.json"
        self.la = self.home / "Library/LaunchAgents"
        self.logs = self.home / "Library/Logs"
        self.ipc = Path(os.environ.get("REAPER_MCP_IPC_DIR")
                        or self.home / "Library/Application Support/reaper-mcp/ipc")
        self.node = self.home / ".local/node"

    def plist(self, label: str) -> Path:
        return self.la / (label + ".plist")


def say(msg: str) -> None:
    print(msg, flush=True)


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
    if os.environ.get("GRS_NO_LAUNCHCTL") or sys.platform != "darwin":
        return 0
    return subprocess.run(["launchctl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def domain() -> str:
    return "gui/%d" % os.getuid()


def loaded(label: str) -> bool:
    if os.environ.get("GRS_NO_LAUNCHCTL") or sys.platform != "darwin":
        return False
    return launchctl("print", "%s/%s" % (domain(), label)) == 0


def stop_agent(p: Paths, label: str, delete: bool = False) -> bool:
    plist = p.plist(label)
    existed = plist.exists()
    launchctl("bootout", "%s/%s" % (domain(), label))
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
    if launchctl("bootstrap", domain(), str(p.plist(label))) != 0:
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
    say("  panel: " + copy_if_changed(need(ROOT / "gamut-chat/GamutChat.lua"), p.scripts / "GamutChat/GamutChat.lua"))


def imgui_installed(p: Paths) -> bool:
    return any((p.resource / "UserPlugins").glob("reaper_imgui-*"))


def install_imgui(p: Paths) -> None:
    if imgui_installed(p):
        say("  ReaImGui: already installed")
        return
    arch = "arm64" if platform.machine() == "arm64" else "x86_64"
    name = "reaper_imgui-%s.dylib" % arch
    url = "https://github.com/cfillion/reaimgui/releases/download/v%s/%s" % (IMGUI_VERSION, name)
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    if hashlib.sha256(data).hexdigest() != IMGUI_SHA256[arch]:
        raise SystemExit("ReaImGui download from %s failed its sha256 check" % url)
    dst = p.resource / "UserPlugins" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.with_name(name + ".part").write_bytes(data)
    os.replace(dst.with_name(name + ".part"), dst)
    say("  ReaImGui %s: installed" % IMGUI_VERSION)


def strip_managed(text: str) -> str:
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith(STARTUP_BEGIN) or line.startswith(LEGACY_STARTUP_BEGIN):
            skip = True
        if not skip:
            out.append(line)
        if line.startswith(STARTUP_END):
            skip = False
    return "".join(out)


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

def ensure_token(p: Paths) -> str:
    p.cfg.mkdir(parents=True, exist_ok=True)
    os.chmod(p.cfg, 0o700)
    if p.token.exists() and p.token.read_text().strip():
        os.chmod(p.token, 0o600)
        return p.token.read_text().strip()
    token = ""
    env = p.cfg / "env"  # written by scripts/macos-host-bootstrap.sh; clients already hold this bearer
    if env.exists():
        m = re.search(r"^REAPER_MCP_BEARER=(\S+)", env.read_text(), re.M)
        token = m.group(1) if m else ""
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
    if os.environ.get("GRS_NO_LAUNCHCTL") or sys.platform != "darwin":
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
    if os.environ.get("GRS_NO_LAUNCHCTL") or sys.platform != "darwin":
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
        return "Gamut isn't running at %s; run `gamut-reaper-suite connect gamut` once it is" % gamut_url()


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
    "claude-desktop": (lambda p: (p.home / "Library/Application Support/Claude").is_dir(),
                       lambda p, remove=False: json_client(
                           p.home / "Library/Application Support/Claude/claude_desktop_config.json", "mcpServers", remove)),
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
    node = p.node / "bin/node"
    if node.exists():
        return p.node / "bin"
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=30) as r:
        version = next(rel["version"] for rel in json.load(r) if rel["lts"])
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    name = "node-%s-darwin-%s" % (version, arch)
    base = "https://nodejs.org/dist/%s/" % version
    with urllib.request.urlopen(base + "SHASUMS256.txt", timeout=30) as r:
        sums = dict(reversed(line.split()) for line in r.read().decode().splitlines() if line.strip())
    say("  installing Node %s into %s" % (version, p.node))
    with urllib.request.urlopen(base + name + ".tar.gz", timeout=300) as r:
        data = r.read()
    if hashlib.sha256(data).hexdigest() != sums.get(name + ".tar.gz"):
        raise SystemExit("Node download failed its sha256 check")
    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp) / "node.tar.gz"
        tar.write_bytes(data)
        with tarfile.open(tar) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(tmp, filter="data")
            else:
                t.extractall(tmp)
        if p.node.exists():
            shutil.rmtree(p.node)
        p.node.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(Path(tmp) / name), str(p.node))
    return p.node / "bin"


def agents_add(p: Paths, slugs: list) -> None:
    unknown = [s for s in slugs if s not in ACP_AGENTS]
    if unknown:
        raise SystemExit("unknown agent(s) %s; choose from %s" % (", ".join(unknown), ", ".join(ACP_AGENTS)))
    bin_dir = ensure_node(p)
    env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ.get("PATH", "/usr/bin:/bin")))
    cfg = load_json(p.gw_cfg) or {"acp": [], "mcpServers": [dict(name="reaper", **stdio_entry())]}
    for slug in slugs:
        a = ACP_AGENTS[slug]
        cmd = [str(bin_dir / "npm"), "install", "-g", "--no-fund", "--no-audit"]
        if a.get("allow_scripts"):
            cmd.append("--allow-scripts=" + a["allow_scripts"])
        say("  npm install %s" % a["package"])
        subprocess.run(cmd + [a["package"]], env=env, check=True)
        path = ":".join([str(bin_dir)] + [str(Path(x).expanduser()) for x in a.get("path_extra", [])]
                        + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        entry = {"slug": slug, "name": a["name"], "description": a["description"],
                 "command": [str(bin_dir / a["bin"])] + a["args"], "env": {"PATH": path},
                 "cwd": "~/Documents/REAPER Media"}
        if a.get("loginVariants"):
            entry["loginVariants"] = a["loginVariants"]
        cfg["acp"] = [x for x in cfg.get("acp", []) if x.get("slug") != slug] + [entry]
    write_json(p.gw_cfg, cfg)
    if loaded(GW_LABEL):
        launchctl("kickstart", "-k", "%s/%s" % (domain(), GW_LABEL))
    say("  added %s; they show up in the panel's agent picker" % ", ".join(slugs))


# ---------- commands ----------

def cmd_setup(a) -> int:
    if sys.platform != "darwin" and not os.environ.get("GRS_NO_LAUNCHCTL"):
        raise SystemExit("setup is macOS only; elsewhere use `reaper-mcp install-bridge` and a stdio MCP config")
    p = Paths(a.resource_path)
    say("gamut-reaper-suite %s from %s (python %s)" % (__version__, ROOT, sys.executable))
    say("REAPER")
    install_bridge(p)
    install_panel(p)
    if not a.no_reaimgui:
        install_imgui(p)
    install_startup(p)
    say("Services")
    ensure_token(p)
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
    say("Done. Restart REAPER: the bridge and the Gamut Chat panel start with it. Check with `gamut-reaper-suite doctor`.")
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
    apps = [Path("/Applications/REAPER.app"), p.home / "Applications/REAPER.app"]
    app = next((x for x in apps if x.exists()), None)
    row("ok" if app else "warn", "REAPER.app", str(app) if app else "not in /Applications or ~/Applications")
    row("ok" if p.resource.is_dir() else "FAIL", "resource path", str(p.resource))
    bv = bridge_version(p.scripts / "reaper_mcp_bridge.lua")
    row("ok" if bv == __version__ else "FAIL", "bridge script",
        "%s (suite %s)" % (bv or "missing", __version__) + ("" if bv == __version__ else " - run setup"))
    panel = p.scripts / "GamutChat/GamutChat.lua"
    shipped = ROOT / "gamut-chat/GamutChat.lua"
    same = panel.exists() and shipped.exists() and panel.read_bytes() == shipped.read_bytes()
    row("ok" if same else "warn", "chat panel", "%s%s" % (panel, "" if same else " (missing or differs from this suite)"))
    row("ok" if imgui_installed(p) else "warn", "ReaImGui", "installed" if imgui_installed(p) else "missing: the panel needs it")
    startup = p.scripts / "__startup.lua"
    hooked = startup.exists() and "reaper_mcp_bridge.lua" in startup.read_text()
    row("ok" if hooked else "FAIL", "startup hook", str(startup) if hooked else "REAPER won't start the bridge on launch")
    if p.token.exists():
        mode = p.token.stat().st_mode & 0o777
        row("ok" if mode == 0o600 else "warn", "token", "%s mode %o" % (p.token, mode))
    else:
        row("FAIL", "token", "%s missing" % p.token)
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
            "running, MCP not registered - `gamut-reaper-suite connect gamut`")
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
    return 0


def cmd_watchdog(a) -> int:
    p = Paths()
    watchdog_on(p) if a.state == "on" else watchdog_off(p)
    return 0


def cmd_agents(a) -> int:
    agents_add(Paths(), a.agents)
    return 0


def cmd_uninstall(a) -> int:
    p = Paths(a.resource_path)
    for label in (MCP_LABEL, GW_LABEL, WD_LABEL, *LEGACY_LABELS):
        if stop_agent(p, label, delete=True):
            say("removed LaunchAgent " + label)
    remove_startup(p)
    for f in (p.scripts / "reaper_mcp_bridge.lua", p.scripts / "reaper_mcp_bridge.lua.bak",
              p.scripts / "reaper_mcp_bridge.lua.new"):
        if f.exists():
            f.unlink()
    shutil.rmtree(p.scripts / "GamutChat", ignore_errors=True)
    say("removed the bridge, the panel and the startup hook (ReaImGui stays: other scripts use it)")
    connect_detected(p, remove=True)
    if a.purge:
        for d in (p.cfg, p.gw_cfg.parent):
            shutil.rmtree(d, ignore_errors=True)
        say("removed %s and %s" % (p.cfg, p.gw_cfg.parent))
    if PKG_ROOT in ROOT.parents or ROOT == PKG_ROOT:
        say("App files: sudo rm -rf '%s' /usr/local/bin/gamut-reaper-suite && sudo pkgutil --forget %s"
            % (PKG_ROOT, PKG_ID))
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gamut-reaper-suite", description="REAPER MCP, Gamut Chat panel and agent gateway.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("setup", help="install or upgrade everything for this macOS user (safe to re-run)")
    s.add_argument("--resource-path", help="REAPER resource path (default: ~/Library/Application Support/REAPER)")
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
    c.set_defaults(fn=cmd_connect)
    w = sub.add_parser("watchdog", help="relaunch REAPER after crashes (for an unattended Mac)")
    w.add_argument("state", choices=["on", "off"])
    w.set_defaults(fn=cmd_watchdog)
    g = sub.add_parser("agents", help="install ACP agents for the panel (needs Node; installed into ~/.local/node)")
    g.add_argument("action", choices=["add"])
    g.add_argument("agents", nargs="+", metavar="|".join(ACP_AGENTS))
    g.set_defaults(fn=cmd_agents)
    u = sub.add_parser("uninstall", help="remove this user's install")
    u.add_argument("--resource-path")
    u.add_argument("--purge", action="store_true", help="also delete tokens, gateway config and ACP transcripts")
    u.set_defaults(fn=cmd_uninstall)
    return ap


def main(argv: list) -> int:
    a = parser().parse_args(argv)
    if not getattr(a, "fn", None):
        parser().print_help()
        return 2
    return a.fn(a)
