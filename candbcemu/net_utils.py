"""Helpers for discovering SocketCAN network interfaces on Linux."""

import os

# From linux/if_arp.h
ARPHRD_CAN = 280


def list_can_interfaces() -> list[str]:
    """Return the names of all CAN (SocketCAN) network interfaces present on the system."""
    net_dir = "/sys/class/net"
    interfaces = []
    try:
        names = os.listdir(net_dir)
    except OSError:
        return interfaces
    for name in sorted(names):
        type_path = os.path.join(net_dir, name, "type")
        try:
            with open(type_path, "r", encoding="ascii") as fh:
                iface_type = fh.read().strip()
        except OSError:
            continue
        if iface_type == str(ARPHRD_CAN):
            interfaces.append(name)
    return interfaces
