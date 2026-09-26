#!/usr/bin/env bash
# Create and bring up a virtual SocketCAN interface for testing without real hardware.
# Requires root (uses modprobe/ip). Usage: sudo ./scripts/setup_vcan.sh [ifname]
set -euo pipefail

IFACE="${1:-vcan0}"

modprobe vcan
ip link add dev "$IFACE" type vcan 2>/dev/null || true
ip link set up "$IFACE"

echo "Virtual CAN interface '$IFACE' is up."
echo "You can watch traffic with: candump $IFACE"
