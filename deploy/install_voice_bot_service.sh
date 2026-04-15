#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=voice-bot.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${SERVICE_NAME}"
DST="$HOME/.config/systemd/user/${SERVICE_NAME}"

mkdir -p "$HOME/.config/systemd/user"
mkdir -p "$HOME/projects/VoxCPM/output"
cp "$SRC" "$DST"

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"

echo "✅ Installed and started $SERVICE_NAME"
echo "   status: systemctl --user status $SERVICE_NAME"
echo "   logs:   journalctl --user -u $SERVICE_NAME -f"
