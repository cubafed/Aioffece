#!/bin/sh
# Connect Claude Code sessions on this Linux box to the ИИ-Офис hub on the Mac.
#   1. systemd --user service keeps an ssh tunnel: 127.0.0.1:4747 here → hub on the Mac (needs the `mac` ssh alias)
#   2. adds the office hook to ~/.claude/settings.json (backup first; undo: python3 deploy/mac/hooks.py uninstall)
#   3. systemd --user service with the runner: office agents (Claude here, where it is logged in) run on this box
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT="$HOME/.config/systemd/user/ai-office-tunnel.service"
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<'EOF'
[Unit]
Description=AI Office: forward 127.0.0.1:4747 to the hub on the Mac
After=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:4747:127.0.0.1:4747 mac
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now ai-office-tunnel.service
sleep 3
curl -sf -o /dev/null http://127.0.0.1:4747/api/state && echo "tunnel up: hub reachable on 127.0.0.1:4747" || echo "tunnel not up yet: systemctl --user status ai-office-tunnel"
python3 "$ROOT/deploy/mac/hooks.py" install

RUNIT="$HOME/.config/systemd/user/ai-office-runner.service"
cat > "$RUNIT" <<EOF
[Unit]
Description=AI Office: run office agents on this box for the hub on the Mac
After=ai-office-tunnel.service

[Service]
ExecStart=/usr/bin/python3 -I $ROOT/hub/runner.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
pkill -f 'hub/runner.py' 2>/dev/null || true      # a runner started by hand would compete for jobs
systemctl --user daemon-reload
systemctl --user enable --now ai-office-runner.service
sleep 2
systemctl --user is-active ai-office-runner.service >/dev/null && echo "runner up: office agents can run on this box"
