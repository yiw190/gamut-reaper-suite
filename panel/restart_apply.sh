#!/bin/sh
# Run while REAPER is open: waits for REAPER to quit, docks docker #2 on the right, binds Cmd+L to the panel,
# then relaunches REAPER (REAPER rewrites its ini files while running, so they can only be edited after quit).
# usage: restart_apply.sh [named action id]   (default: ExtState Hibiki/action, written by the panel)
R="$HOME/Library/Application Support/REAPER"
LOG="$HOME/Library/Logs/hibiki_restart.log"
/bin/mkdir -p "$HOME/Library/Logs"
echo "start $(date)" > "$LOG"
sleep 1
while /usr/bin/pgrep -f "REAPER.app/Contents/MacOS/REAPER" >/dev/null; do sleep 1; done
sleep 1
ID="${1:-$(/usr/bin/awk -F= '/^\[Hibiki\]/{s=1;next} /^\[/{s=0} s&&$1=="action"{print $2}' "$R/reaper-extstate.ini")}"
if [ -z "$ID" ]; then echo "no action id: run Hibiki.lua once first, or pass the id" >> "$LOG"; /usr/bin/open -a REAPER; exit 1; fi
# docker #2 (index 1) -> attached right; right docker width
/usr/bin/perl -0pi -e 's/^dockermode1=.*\n//mg; s/^dockheight_r=.*\n//mg; s/^\[reaper\]\n/[reaper]\ndockermode1=3\ndockheight_r=440\n/m' "$R/reaper.ini"
# Cmd+L toggles Hibiki (main section; 9 = virtkey|Cmd, 76 = L)
KB="$R/reaper-kb.ini"
/usr/bin/grep -v -E '^KEY 9 76 ' "$KB" > "$KB.tmp" && mv "$KB.tmp" "$KB"
echo "KEY 9 76 $ID 0" >> "$KB"
/usr/bin/grep -n -E "dockermode1|dockheight_r" "$R/reaper.ini" >> "$LOG"
/usr/bin/tail -2 "$KB" >> "$LOG"
/usr/bin/open -a REAPER
echo "relaunched $(date)" >> "$LOG"
