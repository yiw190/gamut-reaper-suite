#!/bin/bash
# Install / reload the com.yitian.reaper-watchdog LaunchAgent (runs scripts/reaper-watchdog.sh every 30 s).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=com.yitian.reaper-watchdog
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
chmod +x "$REPO/scripts/reaper-watchdog.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/scripts/reaper-watchdog.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/reaper-watchdog.out</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/reaper-watchdog.out</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST"
uid=$(id -u)
launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$uid" "$PLIST"
launchctl print "gui/$uid/$LABEL" | grep -E "state|run interval" || true
