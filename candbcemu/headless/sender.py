"""Core of the headless sender: cyclic transmission of a DBC's messages with demo data."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import can
import cantools

from ..dbc_utils import _message_key, _mux_selectors
from .patterns import KINDS, Profile, evaluate, profile_for, signal_range

_MIN_CYCLE_MS = 5
_MAX_CYCLE_MS = 3_600_000


@dataclass
class SignalState:
    name: str
    unit: str
    lo: float
    hi: float
    profile: Profile
    is_selector: bool = False
    value: float = 0.0           # last value sent
    auto_kind: str = "sine"      # the demo curve to go back to from "manual"


@dataclass
class MessageState:
    key: int
    message: cantools.database.Message
    cycle_ms: int
    signals: list = field(default_factory=list)
    enabled: bool = False
    selectors: dict = field(default_factory=dict)   # selector signal -> physical values
    mux_index: int = 0
    next_due: float = 0.0
    sent: int = 0
    errors: int = 0
    last_error: str = ""
    raw_hex: str = ""


def _signal_choices(sig) -> list[float]:
    return [float(raw) * sig.scale + sig.offset for raw in sorted(sig.choices)] if sig.choices else []


class Sender:
    def __init__(self, config_path: Optional[str] = None):
        self._lock = threading.RLock()
        self._db = None
        self._dbc_name = ""
        self._messages: dict[int, MessageState] = {}
        self._bus: Optional[can.BusABC] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self._config = self._read_config(config_path)
        self.interface = "socketcan"
        self.channel = "can0"
        self.bitrate = 500000
        self.log: list[str] = []

    # ---- config ----
    @staticmethod
    def _read_config(path: Optional[str]) -> dict:
        if not path or not Path(path).is_file():
            return {}
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return {}

    def _note(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {text}")
        del self.log[:-50]

    # ---- DBC ----
    def load_dbc(self, path: str) -> int:
        db = cantools.database.load_file(path)
        with self._lock:
            was_running = self.running
            enabled_before = {m.message.name for m in self._messages.values() if m.enabled}
            self._db = db
            self._dbc_name = Path(path).name
            self._messages = {}
            msg_cfg = self._config.get("messages", {})
            sig_cfg = self._config.get("signals", {})
            for msg in sorted(db.messages, key=lambda m: m.frame_id):
                cycle = min(_MAX_CYCLE_MS, max(_MIN_CYCLE_MS, int(msg.cycle_time or 100)))
                cycle = int(msg_cfg.get(msg.name, {}).get("cycle", cycle))
                state = MessageState(key=_message_key(msg), message=msg, cycle_ms=cycle)
                state.selectors = {
                    name: [float(i) for i in ids] for name, ids in _mux_selectors(msg).items()
                }
                for sig in msg.signals:
                    lo, hi = signal_range(sig)
                    is_selector = sig.name in state.selectors
                    choices = _signal_choices(sig) if not is_selector else []
                    profile = profile_for(sig, choices)
                    override = sig_cfg.get(sig.name, {})
                    for k in ("kind", "period", "lo", "hi", "phase", "value"):
                        if k in override:
                            setattr(profile, k, override[k] if k == "kind" else float(override[k]))
                    state.signals.append(SignalState(sig.name, sig.unit or "", lo, hi, profile, is_selector,
                                                     auto_kind=profile.kind))
                state.enabled = (was_running and msg.name in enabled_before) or msg_cfg.get(
                    msg.name, {}).get("enabled", False)
                self._messages[state.key] = state
            self._note(f"DBC {self._dbc_name}: {len(self._messages)} messages")
            return len(self._messages)

    # ---- bus ----
    @property
    def running(self) -> bool:
        return self._bus is not None

    def _configure_socketcan(self, channel: str, bitrate: int) -> None:
        # The link may already be up at the right rate (and we may not be root): only complain
        # if it fails and the interface is not usable afterwards.
        for cmd in (["ip", "link", "set", channel, "down"],
                    # restart-ms: leave BUS-OFF by itself (a bus with bit errors would otherwise
                    # keep the interface dead - "Network is down" - until someone restarts it)
                    ["ip", "link", "set", channel, "type", "can", "bitrate", str(bitrate), "restart-ms", "100"],
                    ["ip", "link", "set", channel, "up"]):
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                self._note(f"{' '.join(cmd)}: {result.stderr.strip() or 'failed'}")

    def start(self, interface: str = "socketcan", channel: str = "can0", bitrate: int = 500000,
              set_bitrate: bool = True) -> None:
        self.stop()
        with self._lock:
            if self._db is None:
                raise RuntimeError("no DBC loaded")
            if interface == "socketcan" and set_bitrate and bitrate > 0:
                self._configure_socketcan(channel, bitrate)
            kwargs = {"channel": channel, "interface": interface}
            if interface == "virtual":
                kwargs["receive_own_messages"] = False
            self._bus = can.interface.Bus(**kwargs)
            self.interface, self.channel, self.bitrate = interface, channel, bitrate
            self._stop.clear()
            now = time.monotonic()
            self._t0 = now
            for state in self._messages.values():
                state.next_due = now
            self._thread = threading.Thread(target=self._run, name="can-tx", daemon=True)
            self._thread.start()
            self._note(f"sending on {interface}:{channel}")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            self._thread = None
            if self._bus is not None:
                try:
                    self._bus.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                self._bus = None
                self._note("stopped")

    # ---- cyclic transmission ----
    def _values_for(self, state: MessageState, t: float) -> dict:
        values = {}
        for sig in state.signals:
            sig.value = evaluate(sig.profile, t)
            values[sig.name] = sig.value
        if state.selectors:
            # Multiplexed message: each transmission carries the next selector value.
            for sig in state.signals:
                if sig.is_selector:
                    ids = state.selectors[sig.name]
                    values[sig.name] = ids[state.mux_index % len(ids)]
                    sig.value = values[sig.name]
            state.mux_index += 1
        return values

    def _send_one(self, state: MessageState, t: float) -> None:
        values = self._values_for(state, t)
        try:
            data = state.message.encode(values, padding=True, strict=False)
        except Exception as exc:  # noqa: BLE001
            state.errors += 1
            state.last_error = f"encode: {exc}"
            return
        state.raw_hex = data.hex(" ").upper()
        bus = self._bus
        if bus is None:
            return
        try:
            bus.send(can.Message(arbitration_id=state.message.frame_id, data=data,
                                 is_extended_id=state.message.is_extended_frame), timeout=0.005)
            state.sent += 1
        except Exception as exc:  # noqa: BLE001 - e.g. TX queue full with nobody on the bus
            state.errors += 1
            state.last_error = f"send: {exc}"

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            wake = now + 0.05
            with self._lock:
                for state in self._messages.values():
                    if not state.enabled:
                        continue
                    if state.next_due <= now:
                        self._send_one(state, now - self._t0)
                        state.next_due += state.cycle_ms / 1000.0
                        if state.next_due < now - 0.5:      # fell far behind: do not burst
                            state.next_due = now
                    wake = min(wake, state.next_due)
            self._stop.wait(max(0.001, wake - time.monotonic()))

    # ---- control (used by the web UI) ----
    def set_all_enabled(self, enabled: bool) -> None:
        with self._lock:
            for state in self._messages.values():
                state.enabled = enabled

    def set_message(self, key: int, enabled: Optional[bool] = None, cycle_ms: Optional[int] = None) -> bool:
        with self._lock:
            state = self._messages.get(key)
            if state is None:
                return False
            if enabled is not None:
                state.enabled = bool(enabled)
                state.next_due = time.monotonic()
            if cycle_ms is not None:
                state.cycle_ms = min(_MAX_CYCLE_MS, max(_MIN_CYCLE_MS, int(cycle_ms)))
            return True

    def set_signal(self, key: int, name: str, kind: Optional[str] = None, value: Optional[float] = None,
                   period: Optional[float] = None) -> bool:
        with self._lock:
            state = self._messages.get(key)
            sig = next((s for s in state.signals if s.name == name), None) if state else None
            if sig is None:
                return False
            if kind == "auto":
                kind = sig.auto_kind
            if kind is not None:
                if kind not in KINDS:
                    return False
                if kind in ("manual", "const") and value is None and sig.profile.kind not in ("manual", "const"):
                    sig.profile.value = sig.value       # freeze at the current value
                sig.profile.kind = kind
            if value is not None:
                sig.profile.value = min(sig.hi, max(sig.lo, float(value)))
            if period is not None:
                sig.profile.period = max(0.05, float(period))
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "dbc": self._dbc_name,
                "running": self.running,
                "interface": self.interface,
                "channel": self.channel,
                "bitrate": self.bitrate,
                "log": list(self.log),
                "messages": [
                    {
                        "key": m.key,
                        "name": m.message.name,
                        "id": f"0x{m.message.frame_id:X}",
                        "cycle": m.cycle_ms,
                        "enabled": m.enabled,
                        "sent": m.sent,
                        "errors": m.errors,
                        "lastError": m.last_error,
                        "raw": m.raw_hex,
                        "signals": [
                            {"name": s.name, "unit": s.unit, "lo": s.lo, "hi": s.hi,
                             "kind": s.profile.kind, "value": s.value, "manual": s.profile.value,
                             "period": s.profile.period, "selector": s.is_selector}
                            for s in m.signals
                        ],
                    }
                    for m in self._messages.values()
                ],
            }
