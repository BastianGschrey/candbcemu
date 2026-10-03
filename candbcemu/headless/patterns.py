"""Demo value curves for DBC signals.

Every signal gets a curve derived from its range and its name/unit: engine speed
swings quickly, temperatures drift slowly, enumerations step through their values.
A profile can be overridden per signal (config file or web UI).
"""

from __future__ import annotations

import math
import re
import zlib
from dataclasses import dataclass, field

from ..dbc_utils import _effective_range

# Ranges wider than this are "unbounded" DBC signals - use something that looks sane.
_MAX_SPAN = 1.0e5
_FALLBACK_RANGE = (0.0, 100.0)

KINDS = ("sine", "triangle", "square", "const", "choices", "manual")


@dataclass
class Profile:
    kind: str = "sine"
    period: float = 10.0          # seconds for one cycle (choices: seconds per value)
    lo: float = 0.0               # curve swings between lo and hi
    hi: float = 1.0
    phase: float = 0.0            # 0..1 of a period
    value: float = 0.0            # const / manual
    choices: list = field(default_factory=list)  # physical values for kind "choices"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "period": self.period, "lo": self.lo, "hi": self.hi,
                "phase": self.phase, "value": self.value}


# (regex over "name unit", kind, period s, lowest fraction of the range, highest fraction)
_RULES = [
    (r"rpm|drehzahl|engine ?speed|enginespeed", "sine", 9.0, 0.12, 0.85),
    (r"speed|geschw|kph|km/h|mph", "triangle", 40.0, 0.0, 0.6),
    (r"temp|°c|degc|celsius|coolant|water|oil ?t|egt", "triangle", 80.0, 0.35, 0.8),
    (r"throttle|tps|pedal|accel|load|duty|inj", "triangle", 11.0, 0.02, 0.9),
    (r"boost|map|manifold|press|kpa|bar|psi", "sine", 7.0, 0.1, 0.7),
    (r"batt|volt|^v$| v$", "sine", 30.0, 0.62, 0.7),
    (r"lambda|afr|o2", "sine", 5.0, 0.45, 0.6),
    (r"gear", "square", 6.0, 0.1, 0.6),
    (r"flag|status|warn|alarm|light|lamp|switch|state", "square", 12.0, 0.0, 1.0),
]


def _representable(sig) -> tuple[float, float]:
    """Physical range the raw bits of an integer signal can hold."""
    if sig.is_signed:
        raw_lo, raw_hi = -(1 << (sig.length - 1)), (1 << (sig.length - 1)) - 1
    else:
        raw_lo, raw_hi = 0, (1 << sig.length) - 1
    a, b = raw_lo * sig.scale + sig.offset, raw_hi * sig.scale + sig.offset
    return (a, b) if a <= b else (b, a)


def signal_range(sig) -> tuple[float, float]:
    """Range to generate values in: the DBC's, cut to what the signal's bits can encode
    (some DBCs declare limits that do not fit the bit length or sign)."""
    lo, hi = _effective_range(sig)
    if not sig.is_float:
        r_lo, r_hi = _representable(sig)
        lo, hi = max(lo, r_lo), min(hi, r_hi)
        if lo >= hi:
            lo, hi = r_lo, r_hi
    if hi - lo > _MAX_SPAN or hi <= lo:
        return _FALLBACK_RANGE if not (hi > lo and not sig.is_float) else (lo, min(hi, lo + 100.0))
    return lo, hi


def profile_for(sig, choice_values: list[float] | None = None) -> Profile:
    """Pick a curve for `sig` from its name, unit and range."""
    lo, hi = signal_range(sig)
    # Same signal -> same phase on every run, different signals do not move in lockstep.
    phase = (zlib.crc32(sig.name.encode()) % 1000) / 1000.0

    if choice_values:
        return Profile(kind="choices", period=7.0, lo=lo, hi=hi, phase=phase,
                       value=choice_values[0], choices=list(choice_values))

    text = f"{sig.name} {sig.unit or ''}".lower()
    span = hi - lo
    if span <= 1.0 and sig.length <= 2:  # one or two bit flags
        return Profile(kind="square", period=12.0, lo=lo, hi=hi, phase=phase, value=lo)
    for pattern, kind, period, f_lo, f_hi in _RULES:
        if re.search(pattern, text):
            return Profile(kind=kind, period=period, lo=lo + f_lo * span, hi=lo + f_hi * span,
                           phase=phase, value=lo + f_lo * span)
    return Profile(kind="sine", period=10.0, lo=lo + 0.1 * span, hi=lo + 0.9 * span,
                   phase=phase, value=lo + 0.1 * span)


def evaluate(p: Profile, t: float) -> float:
    """Value of the curve at `t` seconds."""
    if p.kind in ("const", "manual"):
        return p.value
    if p.kind == "choices":
        if not p.choices:
            return p.value
        index = int(t / max(p.period, 0.1) + p.phase * len(p.choices)) % len(p.choices)
        return p.choices[index]

    period = max(p.period, 0.05)
    x = (t / period + p.phase) % 1.0
    if p.kind == "triangle":
        unit = 1.0 - abs(2.0 * x - 1.0)
    elif p.kind == "square":
        unit = 1.0 if x >= 0.5 else 0.0
    else:  # sine
        unit = 0.5 - 0.5 * math.cos(2.0 * math.pi * x)
    return p.lo + unit * (p.hi - p.lo)
