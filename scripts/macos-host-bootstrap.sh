#!/bin/bash
# Remote host: make REAPER on this Mac reachable from an agent elsewhere through a Cloudflare named tunnel.
# Installs the suite from a clone (~/reaper-mcp) with the given bearer, then cloudflared as a LaunchAgent.
# Needs REAPER_MCP_BEARER and CLOUDFLARE_TUNNEL_TOKEN (env or ~/.config/reaper-mcp/env). See docs/remote-host.md.
set -euo pipefail
mkdir -p "$HOME/Library/Logs"
exec > >(tee -a "$HOME/Library/Logs/reaper-mcp-bootstrap.log") 2>&1
echo "=== reaper-mcp bootstrap $(date) ==="
echo "host=$(hostname) arch=$(uname -m) user=$(whoami)"

BIN="$HOME/.local/bin"
CFG="$HOME/.config/reaper-mcp"
LAUNCH="$HOME/Library/LaunchAgents"
REPO="${REAPER_MCP_REPO:-$HOME/reaper-mcp}"
LABEL=com.hibiki.cloudflared
mkdir -p "$BIN" "$CFG" "$LAUNCH"
export PATH="$BIN:$PATH"

if [ -f "$CFG/env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CFG/env"
  set +a
fi
: "${REAPER_MCP_BEARER:?set REAPER_MCP_BEARER}"
: "${CLOUDFLARE_TUNNEL_TOKEN:?set CLOUDFLARE_TUNNEL_TOKEN}"
REAPER_MCP_PORT="${REAPER_MCP_PORT:-8787}"

if [ ! -x "$BIN/cloudflared" ]; then
  echo "installing cloudflared"
  tmp=$(mktemp -d)
  ARCH=$(uname -m); [ "$ARCH" = arm64 ] || ARCH=amd64
  curl -fL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-$ARCH.tgz" -o "$tmp/cf.tgz"
  tar -xzf "$tmp/cf.tgz" -C "$tmp"
  cp "$(find "$tmp" -type f -name 'cloudflared*' | head -1)" "$BIN/cloudflared"
  chmod +x "$BIN/cloudflared"
  rm -rf "$tmp"
fi
"$BIN/cloudflared" --version

if [ ! -d "$REPO/.git" ]; then
  git clone https://github.com/yiw190/hibiki.git "$REPO"
else
  git -C "$REPO" pull --ff-only || echo "WARNING: could not fast-forward $REPO; running the checked-out version" >&2
fi

umask 077
cat > "$CFG/env" << ENVEOF
REAPER_MCP_BEARER=$REAPER_MCP_BEARER
REAPER_MCP_PORT=$REAPER_MCP_PORT
CLOUDFLARE_TUNNEL_TOKEN=$CLOUDFLARE_TUNNEL_TOKEN
ENVEOF
printf '%s\n' "$REAPER_MCP_BEARER" > "$CFG/token"
umask 022

cd "$REPO"
REAPER_MCP_PORT="$REAPER_MCP_PORT" /usr/bin/python3 -m reaper_mcp setup
/usr/bin/python3 -m reaper_mcp watchdog on

uid=$(id -u)
for old in com.yitian.cloudflared-reaper "$LABEL"; do
  launchctl bootout "gui/$uid/$old" 2> /dev/null || true
  rm -f "$LAUNCH/$old.plist"
done
# the tunnel token goes in the environment, not argv: argv is visible to every user via ps
export CLOUDFLARE_TUNNEL_TOKEN
LABEL="$LABEL" BIN="$BIN" /usr/bin/python3 -c '
import os, plistlib
from pathlib import Path
label = os.environ["LABEL"]
log = str(Path.home() / "Library/Logs/cloudflared.log")
path = Path.home() / "Library/LaunchAgents" / (label + ".plist")
fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "wb") as f:
    plistlib.dump({"Label": label, "ProgramArguments": [os.environ["BIN"] + "/cloudflared", "tunnel", "run"],
                   "EnvironmentVariables": {"TUNNEL_TOKEN": os.environ["CLOUDFLARE_TUNNEL_TOKEN"]},
                   "RunAtLoad": True, "KeepAlive": True, "StandardOutPath": log, "StandardErrorPath": log}, f)
'
launchctl bootstrap "gui/$uid" "$LAUNCH/$LABEL.plist"
echo "=== bootstrap done $(date) ==="
