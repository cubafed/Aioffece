#!/bin/zsh
# Install the hub as a launchd agent and build the app. Run on the Mac from ~/ai-office.
# Hooks are a separate, explicit step: python3 deploy/mac/hooks.py install
set -euo pipefail
ROOT="$HOME/ai-office"
LABEL=ru.ai-office.hub
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$ROOT/data" "$HOME/Library/Logs" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$ROOT/hub/server.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/ai-office-hub.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/ai-office-hub.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 1
curl -sf -o /dev/null http://127.0.0.1:4747/api/state && echo "hub is up on http://127.0.0.1:4747"
"$ROOT/app/build.sh"
