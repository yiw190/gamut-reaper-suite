#!/bin/sh
# Install the agent gateway as a LaunchAgent (127.0.0.1:47892).
# WITH_CODEX=1 (default) also installs Node (user-level, ~/.local/node), the Codex + Claude Code ACP adapters
# and Kimi Code CLI (ACP built in; log in from the panel, which opens `kimi login` in Terminal).
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
  echo "installing ACP agents: codex-acp, claude-agent-acp, kimi-code"
  PATH="$NODE_DIR/bin:$PATH" "$NODE_DIR/bin/npm" install -g --no-fund --no-audit \
    --allow-scripts=@moonshot-ai/kimi-code,node-pty \
    @agentclientprotocol/codex-acp @agentclientprotocol/claude-agent-acp @moonshot-ai/kimi-code
  PATH="$NODE_DIR/bin:$PATH" "$NODE_DIR/bin/codex-acp" --version < /dev/null || true
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
    },
    {
      "slug": "claude",
      "name": "Claude Code",
      "description": "Claude Code (Claude Agent SDK) over ACP; uses the local claude login",
      "command": ["$NODE_DIR/bin/claude-agent-acp"],
      "env": {"PATH": "$NODE_DIR/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
      "cwd": "~/Documents/REAPER Media"
    },
    {
      "slug": "kimi",
      "name": "Kimi",
      "description": "Kimi Code CLI over ACP; uses the Kimi Code subscription login",
      "command": ["$NODE_DIR/bin/kimi", "acp"],
      "env": {"PATH": "$NODE_DIR/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
      "loginVariants": [
        {"id": "global", "name": "Log in with kimi.ai", "description": "International account (kimi.ai)", "args": ["--region", "global"]},
        {"id": "cn", "name": "Log in with kimi.com", "description": "Mainland China account (kimi.com)", "args": ["--region", "mainland-cn"]}
      ],
      "cwd": "~/Documents/REAPER Media"
    }
  ],
  "mcpServers": [
    {"name": "reaper", "command": "/usr/bin/python3", "args": ["$REPO/reaper_mcp/server.py"]}
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
for _ in 1 2 3 4 5 6 7 8 9 10; do
  curl -fsS http://127.0.0.1:47892/health 2>/dev/null && echo " gateway up" && exit 0
  sleep 1
done
echo "gateway did not answer; see ~/Library/Logs/agent-gateway.log" >&2
exit 1
