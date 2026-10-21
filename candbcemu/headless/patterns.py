"""Demo values for DBC signals.

Signals that belong to an engine (rpm, speed, gear, throttle, temperatures, boost, lambda ...)
are driven by one shared, endless vehicle simulation (`Engine`): cold start, idle, pulling away,
shifting through the gears, cruising, braking to a stop, and again - with consistent values
(rpm follows speed and gear, load follows the pedal, the coolant warms up slowly ...).
Every other signal gets a plain curve derived from its range and name/unit; enumerations step
through their values. A profile can be overridden per signal (config file or web UI).
"""

from __future__ import annotations

import math
import random
import re
import zlib
from dataclasses import dataclass, field

from ..dbc_utils import _effective_range

# Ranges wider than this are "unbounded" DBC signals - use something that looks sane.
_MAX_SPAN = 1.0e5
_FALLBACK_RANGE = (0.0, 100.0)

KINDS = ("engine", "sine", "triangle", "square", "const", "choices", "manual")


class Engine:
    """Endless vehicle simulation. `get(role, t)` returns a quantity as 0..1 of its reference
    range (see _ROLES); the simulation advances by itself to the requested time."""

    RATIO = (130.0, 75.0, 50.0, 37.0, 29.0, 23.5)   # rpm per km/h in gears 1..6
    ACCEL = (13.0, 9.0, 6.5, 4.8, 3.8, 3.0)         # km/h per second at full throttle
    CRANK_S = 1.6
    STEP = 0.05

    MODES = ("complete", "start", "drive", "idle")   # cold start + driving, cold start + idle only,
                                                     # warm + driving, warm idle (stays at idle)

    def __init__(self, seed: int = 7):
        self._seed = seed
        self.mode = "complete"
        self.reset()

    def set_mode(self, mode: str, t: float = 0.0) -> bool:
        """Switch the scenario and start it over at time `t`."""
        if mode not in self.MODES:
            return False
        self.mode = mode
        self.reset(t)
        return True

    def reset(self, t: float = 0.0) -> None:
        self.rng = random.Random(self._seed)
        self.t = self.t0 = t
        warm = self.mode in ("drive", "idle")
        self.running = warm
        self.rpm = 900.0 if warm else 0.0
        self.v = 0.0                    # km/h
        self.gear = 1
        self.shift_left = 0.0
        self.pedal = 0.0
        self.thr = 0.0                  # throttle plate (cut while shifting)
        self.brake = False
        self.acc = 0.0                  # km/h per second
        self.phase = "idle"
        self.phase_left = 1e9 if self.mode in ("start", "idle") else 4.0
        self.target = 0.0
        self.aggr = 0.7
        self.load = 0.13 if warm else 0.0
        self.map = 0.2 if warm else 0.0
        self.coolant = 92.0 if warm else 20.0
        self.oil = 98.0 if warm else 20.0
        self.egt = 300.0 if warm else 120.0
        self.air = 35.0 if warm else 20.0
        self.fan = False
        self.lam = 0.5
        self.cut = False
        self.wobble = 0.0
        self.q: dict[str, float] = {}
        self._publish()

    # ---- driver ----
    def _next_phase(self) -> None:
        r = self.rng
        if self.phase in ("idle", "cruise"):
            if self.phase == "cruise" and self.v > 20 and r.random() < 0.45:
                self.phase = "decel"
                self.target = 0.0 if r.random() < 0.45 else self.v * r.uniform(0.3, 0.75)
            elif self.mode in ("start", "idle"):
                self.phase_left = 1e9
            else:
                self.phase = "accel"
                self.aggr = r.uniform(0.35, 1.0)
                self.target = max(self.v + 15.0, r.choice((30, 45, 50, 60, 80, 100, 120, 140, 165)))
                self.target = min(self.target, 170.0)
            self.phase_left = 90.0
        elif self.phase == "accel":
            self.phase, self.phase_left = "cruise", r.uniform(6.0, 25.0)
        else:  # decel
            if self.target < 1.0:
                self.phase, self.phase_left = "idle", r.uniform(3.0, 9.0)
            else:
                self.phase, self.phase_left = "cruise", r.uniform(3.0, 12.0)

    def _drive(self, dt: float) -> None:
        self.phase_left -= dt
        v, vt = self.v, self.target
        if self.phase == "accel" and (v >= vt - 2.0 or self.phase_left <= 0):
            self._next_phase()
        elif self.phase == "decel" and (v <= vt + 2.0 or self.phase_left <= 0):
            self._next_phase()
        elif self.phase in ("idle", "cruise") and self.phase_left <= 0:
            self._next_phase()
        g = self.gear
        hold = (0.00011 * v * v + 0.12) / (self.ACCEL[g - 1] * 0.8)    # pedal that keeps the speed
        self.brake = False
        if self.phase == "accel":
            self.pedal = min(self.aggr, max(0.12, (vt - v) * 0.08 + 0.15))
        elif self.phase == "cruise":
            self.pedal = min(1.0, max(0.0, hold + 0.06 * (vt - v) + 0.015 * math.sin(self.t * 0.9)))
        elif self.phase == "decel":
            self.pedal = 0.0
            self.brake = v - vt > 6.0 and v > 3.0
        else:
            self.pedal = 0.0

    # ---- one physics step ----
    def _step(self, dt: float) -> None:
        self.t += dt
        t, r = self.t, self.rng
        if not self.running:
            if t - self.t0 < self.CRANK_S:
                self.rpm = 200.0 + 40.0 * math.sin(t * 25.0)
                self.thr = self.pedal = self.load = 0.0
                self.map = 0.35
                return
            self.running = True
            self.rpm = 1300.0
        self._drive(dt)

        warm = min(1.0, max(0.0, (self.coolant - 20.0) / 65.0))
        idle = 850.0 + 380.0 * (1.0 - warm)                    # cold fast idle
        self.wobble += (r.gauss(0.0, 25.0) - self.wobble) * min(1.0, dt * 3.0)

        # gearbox
        g, v = self.gear, self.v
        rpm_in_gear = v * self.RATIO[g - 1]
        self.shift_left = max(0.0, self.shift_left - dt)
        if self.shift_left <= 0.0:
            new = g
            if v < 8.0:
                new = 1
            elif self.phase != "decel" and rpm_in_gear > 2300.0 + 4200.0 * self.pedal and g < 6:
                new = g + 1
            elif g > 1 and (rpm_in_gear < 1350.0 or (self.pedal > 0.85 and rpm_in_gear < 3000.0)):
                new = g - 1
            if new != g:
                self.gear = new
                self.shift_left = 0.35 if new > g else 0.3
        shifting = self.shift_left > 0.0

        # throttle plate follows the pedal; closed while shifting and at the rev limiter
        self.thr = 0.0 if shifting or self.rpm > 6900.0 else self.pedal
        g = self.gear
        target_rpm = max(idle, v * self.RATIO[g - 1])
        if g == 1 and v < 20.0 and self.thr > 0.05:            # slipping clutch while pulling away
            target_rpm = max(target_rpm, idle + self.thr * 3200.0 * (1.0 - v / 20.0))
        target_rpm += (self.wobble + 12.0 * math.sin(t * 0.7)) if v < 3.0 else 0.0
        tau = 0.1 if target_rpm > self.rpm else 0.28
        self.rpm += (target_rpm - self.rpm) * min(1.0, dt / tau)

        # vehicle
        tq = max(0.3, 0.6 + 0.4 * (1.0 - ((self.rpm - 4500.0) / 4500.0) ** 2))
        engine_brake = 0.0005 * target_rpm if self.pedal < 0.05 and not shifting else 0.0
        resist = (0.00011 * v * v + 0.12) if v > 0.2 else 0.0
        self.acc = self.thr * self.ACCEL[g - 1] * tq - resist - engine_brake - (5.5 if self.brake else 0.0)
        self.v = max(0.0, v + self.acc * dt)

        # engine quantities
        load_t = self.thr * (0.45 + 0.55 * tq) if self.thr > 0.02 else (0.04 if v > 5 else 0.13)
        self.load += (load_t - self.load) * min(1.0, dt / 0.15)
        self.cut = self.pedal < 0.03 and self.rpm > 1700.0 and v > 5.0
        spool = 1.0 if self.rpm > 2200.0 else 0.3
        map_t = 0.16 + 0.84 * (self.load ** 1.5) * spool + 0.008 * math.sin(t * 2.1)
        self.map += (map_t - self.map) * min(1.0, dt / (0.5 if map_t > self.map else 0.25))
        heat = 92.0 + 10.0 * self.load + (6.0 if v < 15.0 else 0.0)
        if self.coolant > 98.0:
            self.fan = True
        elif self.coolant < 93.0:
            self.fan = False
        if self.fan:
            heat -= 9.0
        self.coolant += (heat - self.coolant) * dt / (100.0 if self.coolant < 85.0 else 60.0)
        self.oil += (100.0 + 12.0 * self.load - self.oil) * dt / 150.0
        self.egt += (230.0 + 650.0 * self.load ** 1.2 - self.egt) * dt / 2.5
        self.air += (20.0 + 0.12 * (self.coolant - 20.0) - self.air) * dt / 40.0
        if self.cut:
            lam_t = 1.0
        elif self.load > 0.85:
            lam_t = 0.33
        else:
            lam_t = 0.5 + 0.05 * math.sin(t * 4.8)             # closed loop hunts around stoich
        self.lam += (lam_t - self.lam) * min(1.0, dt / 0.2)

    def _publish(self) -> None:
        run = 1.0 if self.running else 0.0
        clamp = lambda x: min(1.0, max(0.0, x))   # noqa: E731
        ripple = 0.03 * math.sin(self.t * 31.0)
        self.q = {
            "rpm": clamp(self.rpm / 7000.0),
            "speed": clamp(self.v / 200.0),
            "gear": (self.gear - 1) / 5.0,
            "throttle": clamp(self.thr),
            "pedal": clamp(self.pedal),
            "load": clamp(self.load),
            "inj": clamp(self.load * self.rpm / 5000.0),
            "map": clamp(self.map),
            "turbo": clamp((self.map - 0.2) * 1.3) if self.rpm > 1500.0 else 0.0,
            "coolant": clamp((self.coolant - 20.0) / 90.0),
            "oilt": clamp((self.oil - 20.0) / 110.0),
            "egt": clamp((self.egt - 200.0) / 750.0),
            "airt": clamp((self.air - 15.0) / 30.0),
            "volt": clamp(0.93 + ripple - 0.1 * self.load) if self.running else 0.0,
            "lambda": clamp(self.lam),
            "oilp": clamp(0.2 + 0.8 * self.rpm / 7000.0) * run,
            "fuelp": clamp(0.5 + 0.2 * (self.map - 0.2)) * run,
            "ign": clamp(0.8 - 0.55 * self.load + 0.02 * math.sin(self.t * 2.0)),
            "longg": clamp(0.5 + self.acc / 35.3 / 2.0 - (0.1 if self.brake else 0.0)),
            "brakep": 1.0 if self.brake else 0.0,
            "brake": 1.0 if self.brake else 0.0,
            "clutch": 1.0 if self.shift_left > 0.0 else 0.0,
            "neutral": 1.0 if self.v < 1.0 and self.phase == "idle" else 0.0,
            "deccut": 1.0 if self.cut else 0.0,
            "fan": 1.0 if self.fan else 0.0,
            "ignsw": run,
            "baro": 0.5 + 0.01 * math.sin(self.t / 60.0),
        }

    def advance(self, t: float) -> None:
        if t < self.t - 1e-9:                         # time went backwards: start over
            self.reset()
        t = max(t, self.t)
        if t - self.t > 600.0:                        # do not spend ages catching up
            self.t = t - 600.0
        while self.t + self.STEP <= t:
            self._step(self.STEP)
        self._publish()

    def get(self, role: str, t: float) -> float:
        self.advance(t)
        return self.q.get(role, 0.0)


@dataclass
class Profile:
    kind: str = "sine"
    period: float = 10.0          # seconds for one cycle (choices: seconds per value)
    lo: float = 0.0               # curve swings between lo and hi
    hi: float = 1.0
    phase: float = 0.0            # 0..1 of a period
    value: float = 0.0            # const / manual
    choices: list = field(default_factory=list)  # physical values for kind "choices"
    quantize: bool = False        # whole numbers only (gear): the curve steps
    role: str = ""                # kind "engine": which simulated quantity drives the signal
    sim: object = None            # kind "engine": the shared Engine

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
    (r"batt|volt", "sine", 30.0, 0.62, 0.7),
    (r"lambda|afr|o2", "sine", 5.0, 0.45, 0.6),
    (r"gear|gang", "triangle", 36.0, 0.14, 0.86),
    (r"flag|status|warn|alarm|light|lamp|switch|state", "square", 12.0, 0.0, 1.0),
]

# Names that mention an engine quantity but are not the quantity itself.
_SKIP = "slip|wheeldiff|differential|cruise|tqmgmt|gearswitch|ethanol"
_PRESSURES = ("oilp", "fuelp", "brakep")
_TEMPS = ("coolant", "oilt", "egt", "airt")
_JITTER = ("egt", "lambda", "inj")      # several signals of a kind should not be identical

# Signals driven by the Engine simulation: (name spec, role, physical value at 0 and at 1 of the
# role's range, fallback fractions of the signal range when the physical range does not fit,
# whole numbers only). First match wins, so the specific names come first. The spec is matched
# against the lower-case name without "_" and spaces, then a space and the unit (see _matches).
_ROLES = [
    ("brake*switch|brake*pedal", "brake", 0, 1, 0.0, 1.0, True),
    ("^clutch|clutchswitch", "clutch", 0, 1, 0.0, 1.0, True),
    ("neutral", "neutral", 0, 1, 0.0, 1.0, True),
    ("decel*cut|gearcut", "deccut", 0, 1, 0.0, 1.0, True),
    ("fan", "fan", 0, 1, 0.0, 1.0, True),
    ("ignitionswitch", "ignsw", 0, 1, 0.0, 1.0, True),
    ("turbo*speed|turbo*rpm", "turbo", 0, 150000, 0.0, 0.8, False),
    ("driveshaft", "speed", 0, 6400, 0.0, 0.6, False),
    ("enginespeed|<rpm|drehzahl", "rpm", 0, 7000, 0.0, 0.7, False),
    ("oil*press|oilp>|gearbox*press", "oilp", 0, 6, 0.1, 0.7, False),
    ("fuel*press|fuelp>|coolant*press", "fuelp", 0, 6, 0.35, 0.6, False),
    ("brake*press", "brakep", 0, 100, 0.0, 0.8, False),
    ("baro", "baro", 95, 105, 0.45, 0.55, False),
    ("boost|manifold|<map>|kpa|wastegate|press|<bar>|psi", "map", 30, 180, 0.1, 0.7, False),
    ("coolant|water|clt|enginetemp", "coolant", 20, 110, 0.35, 0.8, False),
    ("oil*temp|oilt|gearbox*temp|diff*temp", "oilt", 20, 130, 0.3, 0.75, False),
    ("egt", "egt", 200, 950, 0.15, 0.8, False),
    ("air*temp|iat|intake|fuel*temp|temp", "airt", 15, 45, 0.25, 0.5, False),
    ("wheelspeed|speed|vspd|geschw|kph|km/h|mph", "speed", 0, 200, 0.0, 0.6, False),
    ("gear|gang", "gear", 1, 6, 0.14, 0.86, True),
    ("throttle|tps|dbw", "throttle", 0, 100, 0.0, 0.9, False),
    ("pedal|accel", "pedal", 0, 100, 0.0, 0.9, False),
    ("load|demand", "load", 0, 100, 0.0, 0.9, False),
    ("fuelflow|duty|inj", "inj", 0, 100, 0.0, 0.8, False),
    ("ign*ang|timing|advance", "ign", 5, 40, 0.2, 0.8, False),
    ("afr", "lambda", 11.0, 18.4, 0.3, 0.7, False),
    ("lambda|lamnda|o2", "lambda", 0.75, 1.25, 0.3, 0.7, False),
    ("batt|volt", "volt", 11.5, 14.5, 0.62, 0.7, False),
    ("longitudinal", "longg", -1, 1, 0.35, 0.65, False),
]


def _is_letter(ch: str) -> bool:
    return "a" <= ch <= "z"


def _matches(text: str, spec: str) -> bool:
    """`spec`: alternatives separated by "|". An alternative is parts separated by "*" (found in
    that order); a leading "^" anchors the first part at the start, a leading "<" / trailing ">"
    require a non-letter before the first / after the last part. (The ESP32 firmware has the same
    matcher in dbc.cpp.)"""
    for alt in spec.split("|"):
        anchored = alt.startswith("^")
        alt = alt.lstrip("^")
        before = alt.startswith("<")
        alt = alt.lstrip("<")
        after = alt.endswith(">")
        alt = alt.rstrip(">")
        parts = alt.split("*")
        pos = text.find(parts[0])
        while pos >= 0:
            ok = (not anchored or pos == 0) and (not before or pos == 0 or not _is_letter(text[pos - 1]))
            end = pos + len(parts[0])
            for i, part in enumerate(parts[1:], 1):
                if not ok:
                    break
                at = text.find(part, end)
                while i == len(parts) - 1 and after and at >= 0 and at + len(part) < len(text) \
                        and _is_letter(text[at + len(part)]):
                    at = text.find(part, at + 1)
                ok = at >= 0
                end = at + len(part)
            if ok and len(parts) == 1 and after and end < len(text) and _is_letter(text[end]):
                ok = False
            if ok:
                return True
            pos = text.find(parts[0], pos + 1)
    return False


def signal_text(sig) -> str:
    """What the role rules look at: the lower-case name without "_" / spaces, a space, the unit."""
    return f"{sig.name.replace('_', '').replace(' ', '')} {sig.unit or ''}".lower()


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


def _engine_profile(text: str, lo: float, hi: float, phase: float, sim: Engine,
                    choice_values: list[float] | None, one_bit: bool) -> Profile | None:
    if _matches(text, _SKIP):
        return None
    for pattern, role, p_lo, p_hi, f_lo, f_hi, whole in _ROLES:
        if not _matches(text, pattern):
            continue
        span = hi - lo
        if one_bit and not whole:       # a flag that merely mentions a quantity ("Throttle_Active")
            return None
        kpa = "kpa" in text or "kilopascal" in text
        if "mph" in text and role == "speed":
            p_lo, p_hi = 0, 125
        if role == "speed" and p_hi > 200 and "rpm" not in text:
            p_hi = 200
        if role in _PRESSURES and kpa and role != "brakep":
            p_hi *= 100                                  # bar -> kPa
        if role in _TEMPS and "kelvin" in text:
            p_lo, p_hi = p_lo + 273.15, p_hi + 273.15
        elif role in _TEMPS and "fahrenheit" in text:
            p_lo, p_hi = p_lo * 1.8 + 32, p_hi * 1.8 + 32
        if lo <= p_lo and p_hi <= hi + 1e-9 and not (role == "map" and not kpa):
            c_lo, c_hi = float(p_lo), float(p_hi)
        else:
            c_lo, c_hi = lo + f_lo * span, lo + f_hi * span
        return Profile(kind="engine", lo=c_lo, hi=c_hi, phase=phase, value=c_lo, role=role, sim=sim,
                       quantize=whole, choices=list(choice_values or []))
    return None


def profile_for(sig, choice_values: list[float] | None = None, sim: Engine | None = None) -> Profile:
    """Pick a curve for `sig` from its name, unit and range."""
    lo, hi = signal_range(sig)
    # Same signal -> same phase on every run, different signals do not move in lockstep.
    phase = (zlib.crc32(sig.name.encode()) % 1000) / 1000.0
    text = signal_text(sig)
    span = hi - lo

    if sim is not None and (not choice_values or _matches(text, "^gear|gang")):
        engine = _engine_profile(text, lo, hi, phase, sim, choice_values, span <= 1.0 and sig.length <= 2)
        if engine is not None:
            return engine
    if choice_values:
        return Profile(kind="choices", period=7.0, lo=lo, hi=hi, phase=phase,
                       value=choice_values[0], choices=list(choice_values))

    if span <= 1.0 and sig.length <= 2:  # one or two bit flags
        return Profile(kind="square", period=12.0, lo=lo, hi=hi, phase=phase, value=lo)
    legacy = f"{sig.name} {sig.unit or ''}".lower()
    for pattern, kind, period, f_lo, f_hi in _RULES:
        if re.search(pattern, legacy):
            return Profile(kind=kind, period=period, lo=lo + f_lo * span, hi=lo + f_hi * span,
                           phase=phase, value=lo + f_lo * span, quantize=pattern.startswith("gear"))
    return Profile(kind="sine", period=10.0, lo=lo + 0.1 * span, hi=lo + 0.9 * span,
                   phase=phase, value=lo + 0.1 * span)


def evaluate(p: Profile, t: float) -> float:
    """Value of the curve at `t` seconds."""
    if p.kind in ("const", "manual"):
        return p.value
    if p.kind == "engine" and p.sim is not None:
        unit = p.sim.get(p.role, t)
        if p.role in _JITTER:
            unit = min(1.0, max(0.0, unit + (p.phase - 0.5) * 0.06))
        value = p.lo + unit * (p.hi - p.lo)
        if p.choices:                                   # enumeration: nearest listed value
            return min(p.choices, key=lambda c: abs(c - value))
        return float(round(value)) if p.quantize else value
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
    value = p.lo + unit * (p.hi - p.lo)
    return float(round(value)) if p.quantize else value
