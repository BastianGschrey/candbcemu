#!/usr/bin/env bash
# Launcher for the CAN DBC Emulator GUI.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/main.py" "$@"
