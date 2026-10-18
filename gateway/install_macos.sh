#!/bin/sh
# Install the agent gateway as a LaunchAgent (127.0.0.1:47892).
# WITH_CODEX=1 (default) also installs Node (user-level, ~/.local/node) and @agentclientprotocol/codex-acp.
# Re-run to upgrade; an existing ~/.config/agent-gateway/config.json is kept.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$HOME/.config/agent-gateway"
LA="$HOME/Library/LaunchAgents"
LABEL=com.gamut-reaper-suite.agent-gateway
NODE_DIR="$HOME/.local/node"
mkdir -p "$CFG" "$LA" "$HOME/Library/Logs"

if [ "${WITH_CODEX:-1}" = 1 ]; then
  if [ ! -x "$NODE_DIR/bin/node" ]; then
    V=$(curl -fsSL https://nodejs.org/dist/index.json |
      /usr/bin/python3 -c 'import json,sys; print(next(r["version"] for r in json.load(sys.stdin) if r["lts"]))')
    ARCH=$(uname -m); [ "$ARCH" = x86_64 ] && ARCH=x64
    echo "installing node $V ($ARCH) into $NODE_DIR"
    TMP=$(mktemp -d)
    curl -fsSL "https://nodejs.org/dist/$V/node-$V-darwin-$ARCH.tar.gz" | tar -xz -C "$TMP"
    rm -rf "$NODE_DIR"
    mkdir -p "$(dirname "$NODE_DIR")"
    mv "$TMP/node-$V-darwin-$ARCH" "$NODE_DIR"
    rm -rf "$TMP"
  fi
  echo "installing @agentclientprotocol/codex-acp"
  PATH="$NODE_DIR/bin:$PATH" "$NODE_DIR/bin/npm" install -g --no-fund --no-audit @agentclientprotocol/codex-acp
  PATH="$NODE_DIR/bin:$PATH" "$NODE_DIR/bin/codex-acp" --version || true
fi

if [ ! -f "$CFG/config.json" ]; then
  cat > "$CFG/config.json" << EOF
{
  "acp": [
    {
      "slug": "codex",
      "name": "Codex",
      "description": "OpenAI Codex CLI over ACP",
      "command": ["$NODE_DIR/bin/codex-acp"],
      "env": {"PATH": "$NODE_DIR/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
      "cwd": "~/Documents/REAPER Media"
    }
  ],
  "mcpServers": [
    {"name": "reaper", "command": "/usr/bin/python3", "args": ["$REPO/python/server.py"]}
  ]
}
EOF
  echo "wrote $CFG/config.json"
fi

cat > "$LA/$LABEL.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$REPO/gateway/agent_gateway.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/agent-gateway.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/agent-gateway.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LA/$LABEL.plist"
sleep 1
curl -fsS http://127.0.0.1:47892/health && echo " gateway up"
