#!/bin/sh
# Schedule ailog.py daily at 9 a.m. on this Mac with launchd. Run once per machine.
#   ./install.sh            install (or reinstall) and run once now
#   ./install.sh --remove   unschedule
# launchd rather than cron: a run missed while the Mac slept happens on wake.
# 9 a.m. is the quietest hour for prompts, so live sessions are rarely mid-turn.
set -e
LABEL=com.jsundram.ai-log
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE=$(cd "$(dirname "$0")" && pwd)

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "$1" = "--remove" ]; then
    trash "$PLIST" 2>/dev/null || true
    echo "removed $LABEL"
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>-I</string>
        <string>$HERE/ailog.py</string>
        <string>-q</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <key>RunAtLoad</key><true/>
    <key>LowPriorityIO</key><true/>
    <key>Nice</key><integer>10</integer>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/ai-log.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/ai-log.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; log: ~/Library/Logs/ai-log.log"
