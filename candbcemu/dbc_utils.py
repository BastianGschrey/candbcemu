"""Qt-free helpers for reading DBC databases (shared by the GUI and the headless sender)."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from cantools.database import Message as DbcMessage


def _effective_range(sig) -> tuple[float, float]:
    if sig.minimum is not None and sig.maximum is not None:
        return float(sig.minimum), float(sig.maximum)
    if sig.is_float:
        lo = sig.minimum if sig.minimum is not None else -1.0e6
        hi = sig.maximum if sig.maximum is not None else 1.0e6
        return float(lo), float(hi)
    if sig.is_signed:
        raw_min = -(1 << (sig.length - 1))
        raw_max = (1 << (sig.length - 1)) - 1
    else:
        raw_min = 0
        raw_max = (1 << sig.length) - 1
    lo = sig.minimum if sig.minimum is not None else raw_min * sig.scale + sig.offset
    hi = sig.maximum if sig.maximum is not None else raw_max * sig.scale + sig.offset
    return (float(lo), float(hi)) if lo <= hi else (float(hi), float(lo))


def _decimals_for_scale(scale: float) -> int:
    """Number of fractional digits needed to display multiples of `scale` exactly."""
    if not scale:
        return 0
    exponent = Decimal(repr(abs(float(scale)))).normalize().as_tuple().exponent
    return min(6, max(0, -exponent))


_EXTENDED_KEY_FLAG = 1 << 30  # extended IDs use at most 29 bits, so this stays within QML's int range


def _message_key(msg: DbcMessage) -> int:
    """Unique per-message key: a standard and an extended frame may share the same ID."""
    return msg.frame_id | (_EXTENDED_KEY_FLAG if msg.is_extended_frame else 0)


def _initial_physical(sig) -> Optional[float]:
    raw = getattr(sig, "raw_initial", None)
    if raw is not None:
        return float(raw) * sig.scale + sig.offset
    try:
        return float(sig.initial) if sig.initial is not None else None
    except (TypeError, ValueError):
        return None


def _mux_selectors(msg: DbcMessage) -> dict[str, list[int]]:
    """Map each multiplexer selector signal to the selector values the DBC defines."""
    selectors: dict[str, list[int]] = {}

    def walk(nodes):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for selector, branches in node.items():
                selectors[selector] = sorted(int(i) for i in branches)
                for children in branches.values():
                    walk(children)

    walk(msg.signal_tree)
    return selectors
