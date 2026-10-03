#!/usr/bin/env bash
# Set up a Raspberry Pi (Raspberry Pi OS Lite) with a PiCAN2 as headless CAN demo sender.
# Run once on the Pi as root, from a copy of this repository:  sudo ./scripts/pi-install.sh [dbc-name]
# Afterwards the sender starts at boot; the web UI is on http://<pi-ip>:8080/
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DBC="${1:-emu_black.dbc}"
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot        # Bookworm: /boot/firmware, older: /boot
CONFIG="$BOOT/config.txt"

apt-get update
apt-get install -y python3-venv can-utils

# Python environment next to the code, only what the sender needs (no Qt).
python3 -m venv "$HERE/.venv-headless"
"$HERE/.venv-headless/bin/pip" install -r "$HERE/requirements-headless.txt"

# PiCAN2: MCP2515 on SPI0 with a 16 MHz crystal, interrupt on GPIO25.
add_line() { grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"; }
add_line "dtparam=spi=on"
add_line "dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25"

cat > /etc/systemd/system/candbcemu-headless.service <<UNIT
[Unit]
Description=CAN demo sender (candbcemu headless)
After=network.target

[Service]
WorkingDirectory=$HERE
ExecStart=$HERE/.venv-headless/bin/python -m candbcemu.headless --dbc $DBC --channel can0 --bitrate 500000 --auto
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable candbcemu-headless.service

echo
echo "Done. Reboot now (the CAN overlay is loaded at boot):  sudo reboot"
echo "Then: ip -details link show can0   and   http://$(hostname -I | awk '{print $1}'):8080/"
