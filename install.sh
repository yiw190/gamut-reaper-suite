#!/bin/sh
# One-step macOS install for a Mac that runs the Gamut desktop app: REAPER bridge, Gamut Chat panel, ReaImGui,
# REAPER startup hook, the reaper MCP over HTTP on 127.0.0.1 (LaunchAgent + bearer token) and its registration in
# Gamut. WITH_AGENTS=1 also installs the agent gateway (Codex, Claude Code, Kimi). Safe to re-run.
set -eu
REPO="$(cd "$(dirname "$0")" && pwd)"
RES="${REAPER_RESOURCE_PATH:-$HOME/Library/Application Support/REAPER}"
CFG="$HOME/.config/reaper-mcp"
LA="$HOME/Library/LaunchAgents"
LABEL=com.gamut-reaper-suite.reaper-mcp
PORT="${REAPER_MCP_PORT:-8787}"
GAMUT="${GAMUT_URL:-http://127.0.0.1:47891}"
IMGUI_VERSION=0.10.0.5

[ "$(uname)" = Darwin ] || { echo "install.sh is macOS only" >&2; exit 1; }
mkdir -p "$RES/Scripts/GamutChat" "$RES/UserPlugins" "$CFG" "$LA" "$HOME/Library/Logs"

echo "1/6 REAPER bridge"
REAPER_RESOURCE_PATH="$RES" /usr/bin/python3 "$REPO/reaper_mcp/install_bridge.py" > /dev/null

echo "2/6 Gamut Chat panel"
cp "$REPO/gamut-chat/GamutChat.lua" "$RES/Scripts/GamutChat/GamutChat.lua"

echo "3/6 ReaImGui"
if ls "$RES/UserPlugins"/reaper_imgui-*.dylib > /dev/null 2>&1; then
  echo "  already installed"
else
  ARCH=$(uname -m); [ "$ARCH" = arm64 ] || ARCH=x86_64
  curl -fsSL -o "$RES/UserPlugins/reaper_imgui-$ARCH.dylib.part" \
    "https://github.com/cfillion/reaimgui/releases/download/v$IMGUI_VERSION/reaper_imgui-$ARCH.dylib"
  mv "$RES/UserPlugins/reaper_imgui-$ARCH.dylib.part" "$RES/UserPlugins/reaper_imgui-$ARCH.dylib"
fi

echo "4/6 REAPER startup hook"
S="$RES/Scripts/__startup.lua"
touch "$S"
/usr/bin/awk '/^-- >>> gamut-reaper-suite/{skip=1} !skip{print} /^-- <<< gamut-reaper-suite/{skip=0}' "$S" > "$S.tmp"
if grep -q reaper_mcp_bridge.lua "$S.tmp"; then
  # a hand-copied hook already starts the bridge; a second start would make REAPER ask to stop the running script
  rm "$S.tmp"
  echo "  $S already starts the bridge; left as is"
else
  [ -s "$S.tmp" ] && [ -n "$(tail -c1 "$S.tmp")" ] && echo >> "$S.tmp"
  { cat "$S.tmp"; echo "-- >>> gamut-reaper-suite (managed by install.sh)"
    cat "$REPO/reaper/__startup.lua"; echo "-- <<< gamut-reaper-suite"; } > "$S"
  rm "$S.tmp"
fi

echo "5/6 reaper MCP on 127.0.0.1:$PORT"
umask 077
[ -s "$CFG/token" ] || /usr/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$CFG/token"
cat > "$LA/$LABEL.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$REPO/reaper_mcp/http_server.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>REAPER_MCP_BEARER</key><string>$(cat "$CFG/token")</string>
    <key>REAPER_MCP_PORT</key><string>$PORT</string>
  </dict>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/reaper-mcp-http.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/reaper-mcp-http.log</string>
</dict>
</plist>
EOF
umask 022
launchctl bootout "gui/$(id -u)/$LABEL" 2> /dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LA/$LABEL.plist"
up=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  curl -fsS "http://127.0.0.1:$PORT/healthz" > /dev/null 2>&1 && up=1 && break
  sleep 1
done
[ "$up" = 1 ] || { echo "reaper MCP did not start; see ~/Library/Logs/reaper-mcp-http.log" >&2; exit 1; }

echo "6/6 register in Gamut"
/usr/bin/python3 - "$GAMUT" "http://localhost:$PORT/mcp" "$CFG/token" << 'PY'
import json, sys, urllib.error, urllib.request

gamut, url, token_file = sys.argv[1:]
token = open(token_file).read().strip()


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(gamut + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


try:
    servers = call("GET", "/api/remote-mcps")["servers"]
    auth = {"authType": "bearer", "accessToken": token}
    old = next((s for s in servers if s.get("url") == url), None)
    if old:
        call("PATCH", "/api/remote-mcps/" + old["id"], auth)
        print("  updated MCP server '%s'" % old.get("name"))
    else:
        call("POST", "/api/remote-mcps", dict(auth, name="REAPER", url=url))
        print("  added MCP server 'REAPER'")
except urllib.error.HTTPError as e:
    sys.exit("  Gamut refused the MCP server (%s): %s" % (e.code, e.read().decode(errors="replace")))
except OSError:
    print("  Gamut isn't running at %s. Re-run install.sh once it is, or add a remote MCP server in Gamut:" % gamut)
    print("    URL %s, bearer token: pbcopy < %s" % (url, token_file))
PY

if [ "${WITH_AGENTS:-0}" = 1 ]; then
  sh "$REPO/gateway/install_macos.sh"
fi

echo
echo "Done. Restart REAPER, then in Gamut give an agent the 'REAPER' MCP server."
