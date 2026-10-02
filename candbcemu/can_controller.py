"""Qt/QML-facing controller that ties a DBC database to a live python-can bus."""

from __future__ import annotations

import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import can
import cantools
from cantools.database import Message as DbcMessage
from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Property, Qt, QTimer, Signal, Slot

from .net_utils import list_can_interfaces


def _local_path(path: str) -> str:
    """QML file dialogs hand back file:// URLs; slots want plain filesystem paths."""
    if path.startswith("file://"):
        return unquote(urlparse(path).path)
    return path


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
_MAX_CYCLE_MS = 3_600_000


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


_RX_QUEUE_MAX = 2000
_RX_FLUSH_MS = 50
_RX_LINES_PER_FLUSH = 100


class _RxListener(can.Listener):
    """Collects frames on the python-can notifier thread; the GUI thread drains them."""

    def __init__(self):
        self.queue: deque = deque(maxlen=_RX_QUEUE_MAX)
        self.dropped = 0

    def on_message_received(self, msg: can.Message) -> None:
        if len(self.queue) == self.queue.maxlen:
            self.dropped += 1
        self.queue.append(msg)

    def on_error(self, exc: Exception) -> None:
        self.queue.append(exc)


@dataclass
class TxState:
    message: DbcMessage
    values: dict = field(default_factory=dict)
    task: Optional[can.broadcastmanager.CyclicSendTaskABC] = None


class MessageListModel(QAbstractListModel):
    NameRole = Qt.UserRole + 1
    MsgIdRole = Qt.UserRole + 2
    MsgIdHexRole = Qt.UserRole + 3
    DlcRole = Qt.UserRole + 4
    CommentRole = Qt.UserRole + 5
    CycleTimeRole = Qt.UserRole + 6
    TransmitEnabledRole = Qt.UserRole + 7
    RawHexRole = Qt.UserRole + 8
    SigListRole = Qt.UserRole + 9
    ExtendedRole = Qt.UserRole + 10

    _ROLE_NAMES = {
        NameRole: b"name",
        MsgIdRole: b"msgId",
        MsgIdHexRole: b"msgIdHex",
        DlcRole: b"dlc",
        CommentRole: b"comment",
        CycleTimeRole: b"cycleTime",
        TransmitEnabledRole: b"transmitEnabled",
        RawHexRole: b"rawHex",
        SigListRole: b"sigList",
        ExtendedRole: b"extended",
    }

    _FIELD_BY_ROLE = {
        NameRole: "name",
        MsgIdRole: "msg_id",
        MsgIdHexRole: "msg_id_hex",
        DlcRole: "dlc",
        CommentRole: "comment",
        CycleTimeRole: "cycle_time",
        TransmitEnabledRole: "transmit_enabled",
        RawHexRole: "raw_hex",
        SigListRole: "sig_list",
        ExtendedRole: "extended",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._row_by_id: dict[int, int] = {}

    def roleNames(self):
        return self._ROLE_NAMES

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role):
        if not index.isValid():
            return None
        field_name = self._FIELD_BY_ROLE.get(role)
        if field_name is None:
            return None
        return self._rows[index.row()].get(field_name)

    def reset(self, rows: list[dict]):
        self.beginResetModel()
        self._rows = rows
        self._row_by_id = {row["msg_id"]: i for i, row in enumerate(rows)}
        self.endResetModel()

    def find_row_by_id(self, msg_id: int) -> int:
        return self._row_by_id.get(msg_id, -1)

    def update_field(self, row: int, field_name: str, value, role: int):
        if self._rows[row].get(field_name) == value:
            return
        self._rows[row][field_name] = value
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [role])


class CanController(QObject):
    dbcLoaded = Signal(str, int)
    dbcError = Signal(str)
    connectionChanged = Signal(bool)
    connectionError = Signal(str)
    logMessage = Signal(str)
    availableInterfacesChanged = Signal()
    busyChanged = Signal()
    vcanCreated = Signal(str)
    vcanFailed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = MessageListModel(self)
        self._db: Optional[cantools.database.Database] = None
        self._bus: Optional[can.BusABC] = None
        self._tx_states: dict[int, TxState] = {}
        self._connected = False
        self._dbc_name = ""
        self._busy = False
        self._notifier: Optional[can.Notifier] = None
        self._rx_listener: Optional[_RxListener] = None
        self._rx_logging = True
        self._rx_timer = QTimer(self)
        self._rx_timer.setInterval(_RX_FLUSH_MS)
        self._rx_timer.timeout.connect(self._flush_rx)

    # -- properties ---------------------------------------------------
    @Property(QObject, constant=True)
    def messagesModel(self):
        return self._model

    @Property(bool, notify=connectionChanged)
    def connected(self):
        return self._connected

    @Property(str, notify=dbcLoaded)
    def dbcName(self):
        return self._dbc_name

    @Property(bool, notify=busyChanged)
    def busy(self):
        return self._busy

    @Property("QVariantList", notify=availableInterfacesChanged)
    def availableInterfaces(self):
        return list_can_interfaces()

    @Property(str, constant=True)
    def vcanScriptPath(self):
        return str(Path(__file__).resolve().parent.parent / "scripts" / "setup_vcan.sh")

    # -- interface discovery -------------------------------------------
    @Slot()
    def refreshInterfaces(self):
        self.availableInterfacesChanged.emit()

    # -- DBC loading -----------------------------------------------------
    @Slot(str, result=bool)
    def loadDbc(self, path: str) -> bool:
        local_path = _local_path(path)
        try:
            db = cantools.database.load_file(local_path)
        except Exception as exc:  # noqa: BLE001 - surface any parser failure to the UI
            self.dbcError.emit(f"Failed to load {local_path}: {exc}")
            return False

        self._stop_all_periodic()
        self._db = db
        self._tx_states = {}
        rows = []
        for msg in sorted(db.messages, key=lambda m: m.frame_id):
            values = {}
            sig_list = []
            mux_ids = _mux_selectors(msg)
            for sig in msg.signals:
                minimum, maximum = _effective_range(sig)
                if sig.name in mux_ids:
                    # Only the selector values the DBC defines encode successfully.
                    choice_values = [float(i) * sig.scale + sig.offset for i in mux_ids[sig.name]]
                    choices = [
                        {"value": value, "name": str(sig.choices.get(i, i)) if sig.choices else str(i)}
                        for i, value in zip(mux_ids[sig.name], choice_values)
                    ]
                elif sig.choices:
                    # DBC value tables are keyed by raw value; signals carry physical values.
                    choice_values = [float(raw) * sig.scale + sig.offset for raw in sorted(sig.choices)]
                    choices = [
                        {"value": value, "name": str(sig.choices[raw])}
                        for raw, value in zip(sorted(sig.choices), choice_values)
                    ]
                else:
                    choice_values = []
                    choices = []

                initial = _initial_physical(sig)
                if initial is None:
                    initial = 0.0 if minimum <= 0.0 <= maximum else minimum
                if choice_values and not any(abs(initial - v) < 1e-9 for v in choice_values):
                    initial = choice_values[0]
                values[sig.name] = initial
                sig_list.append({
                    "name": sig.name,
                    "unit": sig.unit or "",
                    "minimum": minimum,
                    "maximum": maximum,
                    "initial": initial,
                    "decimals": _decimals_for_scale(sig.scale),
                    "step": abs(sig.scale) if sig.scale else 1.0,
                    "choices": choices,
                    "comment": sig.comment or "",
                })
            rows.append({
                "name": msg.name,
                "msg_id": _message_key(msg),
                "msg_id_hex": f"0x{msg.frame_id:X}",
                "dlc": msg.length,
                "comment": msg.comment or "",
                "cycle_time": min(_MAX_CYCLE_MS, max(1, msg.cycle_time or 100)),
                "transmit_enabled": False,
                "raw_hex": "",
                "sig_list": sig_list,
                "extended": bool(msg.is_extended_frame),
            })
            self._tx_states[_message_key(msg)] = TxState(message=msg, values=values)

        self._model.reset(rows)
        for row_idx in range(len(rows)):
            self._refresh_raw_hex(row_idx)

        self._dbc_name = Path(local_path).name
        self.dbcLoaded.emit(self._dbc_name, len(rows))
        self.logMessage.emit(f"Loaded {self._dbc_name}: {len(rows)} messages")
        return True

    # -- bus connection ------------------------------------------------
    @Slot(str, str, int, result=bool)
    def connectBus(self, iface_type: str, channel: str, bitrate: int) -> bool:
        if iface_type == "slcan" and bitrate <= 0:
            self.connectionError.emit("Invalid bitrate")
            self.logMessage.emit("Connection failed: invalid bitrate")
            return False
        if self._bus is not None:
            self.disconnectBus()
        try:
            if iface_type == "socketcan":
                bus = can.interface.Bus(channel=channel, interface="socketcan")
            elif iface_type == "slcan":
                bus = can.interface.Bus(channel=channel, interface="slcan", bitrate=bitrate)
            elif iface_type == "virtual":
                bus = can.interface.Bus(
                    channel=channel or "virtual-emu",
                    interface="virtual",
                    receive_own_messages=False,
                )
            else:
                raise ValueError(f"Unsupported interface type '{iface_type}'")
        except Exception as exc:  # noqa: BLE001
            self.connectionError.emit(str(exc))
            self.logMessage.emit(f"Connection failed: {exc}")
            return False

        self._bus = bus
        self._rx_listener = _RxListener()
        self._notifier = can.Notifier(bus, [self._rx_listener])
        self._rx_timer.start()
        self._connected = True
        self.connectionChanged.emit(True)
        self.logMessage.emit(f"Connected: {iface_type} channel='{channel}' bitrate={bitrate}")
        return True

    @Slot()
    def disconnectBus(self):
        self._stop_all_periodic()
        self._rx_timer.stop()
        if self._notifier is not None:
            try:
                self._notifier.stop()
            except Exception as exc:  # noqa: BLE001
                self.logMessage.emit(f"Error while stopping receiver: {exc}")
            self._notifier = None
        self._rx_listener = None
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception as exc:  # noqa: BLE001
                self.logMessage.emit(f"Error while shutting down bus: {exc}")
        self._bus = None
        if self._connected:
            self._connected = False
            self.connectionChanged.emit(False)
            self.logMessage.emit("Disconnected")
        for row in range(self._model.rowCount()):
            self._model.update_field(row, "transmit_enabled", False, MessageListModel.TransmitEnabledRole)

    @Slot(str, int)
    def applySocketcanBitrate(self, channel: str, bitrate: int):
        """Bring a SocketCAN link down, set its bitrate, and bring it back up.

        Requires CAP_NET_ADMIN (typically root) - reports failure via logMessage
        rather than silently retrying with elevated privileges. Runs off the GUI thread.
        """
        if bitrate <= 0:
            self.logMessage.emit("Invalid bitrate")
            return
        commands = [
            ["ip", "link", "set", channel, "down"],
            ["ip", "link", "set", channel, "type", "can", "bitrate", str(bitrate)],
            ["ip", "link", "set", channel, "up"],
        ]

        def work():
            if self._run_commands(commands):
                self.logMessage.emit(f"{channel} configured at {bitrate} bps")
                self.availableInterfacesChanged.emit()

        self._run_async(work)

    @Slot(str)
    def createVcanInterface(self, name: str):
        """Load the vcan kernel module and bring up a virtual SocketCAN link.

        Equivalent to scripts/setup_vcan.sh - requires CAP_NET_ADMIN (root).
        Safe to call on an interface that already exists. Runs off the GUI thread
        and reports the outcome via vcanCreated / vcanFailed.
        """
        name = name.strip() or "vcan0"
        commands = [
            ["modprobe", "vcan"],
            ["ip", "link", "add", "dev", name, "type", "vcan"],
            ["ip", "link", "set", "up", name],
        ]

        def work():
            if self._run_commands(commands, tolerate={1: "File exists"}):
                self.logMessage.emit(f"Virtual CAN interface '{name}' is up")
                self.availableInterfacesChanged.emit()
                self.vcanCreated.emit(name)
            else:
                self.vcanFailed.emit(name)

        self._run_async(work)

    # -- receiving -------------------------------------------------------
    @Slot(bool)
    def setRxLogging(self, enabled: bool):
        self._rx_logging = enabled

    # -- signal / message control ---------------------------------------
    @Slot(int, str, float)
    def setSignalValue(self, msg_id: int, signal_name: str, value: float):
        state = self._tx_states.get(msg_id)
        if state is None:
            return
        state.values[signal_name] = value
        self._reencode(msg_id, push_to_task=True)

    @Slot(int, int)
    def setCycleTime(self, msg_id: int, milliseconds: int):
        row = self._model.find_row_by_id(msg_id)
        if row < 0:
            return
        self._model.update_field(row, "cycle_time", max(1, milliseconds), MessageListModel.CycleTimeRole)
        state = self._tx_states.get(msg_id)
        if state and state.task is not None:
            self.setTransmitEnabled(msg_id, False)
            self.setTransmitEnabled(msg_id, True)

    @Slot(int, bool)
    def setTransmitEnabled(self, msg_id: int, enabled: bool):
        state = self._tx_states.get(msg_id)
        row = self._model.find_row_by_id(msg_id)
        if state is None or row < 0:
            return
        if enabled:
            if self._bus is None:
                self.logMessage.emit("Not connected to a CAN bus")
                self._model.update_field(row, "transmit_enabled", False, MessageListModel.TransmitEnabledRole)
                return
            data = self._reencode(msg_id, push_to_task=False)
            if data is None:
                return
            can_msg = can.Message(
                arbitration_id=state.message.frame_id,
                data=data,
                is_extended_id=state.message.is_extended_frame,
            )
            if state.task is not None:
                state.task.stop()
                state.task = None
            period_s = self._model._rows[row]["cycle_time"] / 1000.0
            try:
                state.task = self._bus.send_periodic(can_msg, period_s)
            except Exception as exc:  # noqa: BLE001
                self.logMessage.emit(f"Failed to start periodic TX for {state.message.name}: {exc}")
                return
            self.logMessage.emit(f"TX start: {state.message.name} every {period_s * 1000:.0f} ms")
        else:
            if state.task is not None:
                try:
                    state.task.stop()
                except Exception:  # noqa: BLE001
                    pass
                state.task = None
                self.logMessage.emit(f"TX stop: {state.message.name}")
        self._model.update_field(row, "transmit_enabled", enabled, MessageListModel.TransmitEnabledRole)

    @Slot(int)
    def sendOnce(self, msg_id: int):
        state = self._tx_states.get(msg_id)
        if state is None:
            return
        if self._bus is None:
            self.logMessage.emit("Not connected to a CAN bus")
            return
        data = self._reencode(msg_id, push_to_task=True)
        if data is None:
            return
        can_msg = can.Message(
            arbitration_id=state.message.frame_id,
            data=data,
            is_extended_id=state.message.is_extended_frame,
        )
        try:
            self._bus.send(can_msg)
            self.logMessage.emit(f"Sent {state.message.name}: {data.hex(' ').upper()}")
        except Exception as exc:  # noqa: BLE001
            self.logMessage.emit(f"Send failed for {state.message.name}: {exc}")

    # -- internals -------------------------------------------------------
    def _flush_rx(self):
        listener = self._rx_listener
        if listener is None:
            return
        if listener.dropped:
            dropped, listener.dropped = listener.dropped, 0
            if self._rx_logging:
                self.logMessage.emit(f"RX overflow: {dropped} frames dropped")
        for _ in range(_RX_LINES_PER_FLUSH):
            try:
                item = listener.queue.popleft()
            except IndexError:
                break
            if not self._rx_logging:
                continue
            if isinstance(item, Exception):
                self.logMessage.emit(f"RX error: {item}")
            else:
                self.logMessage.emit(self._format_rx(item))

    def _format_rx(self, msg: can.Message) -> str:
        if msg.is_error_frame:
            return "RX error frame"
        id_text = f"0x{msg.arbitration_id:X}" + (" (ext)" if msg.is_extended_id else "")
        if msg.is_remote_frame:
            return f"RX {id_text} remote request [{msg.dlc}]"
        data = bytes(msg.data)
        line = f"RX {id_text} [{len(data)}] {data.hex(' ').upper()}"
        key = msg.arbitration_id | (_EXTENDED_KEY_FLAG if msg.is_extended_id else 0)
        state = self._tx_states.get(key)
        if state is None:
            return line
        try:
            decoded = state.message.decode(data, allow_truncated=True)
        except Exception as exc:  # noqa: BLE001
            return f"{line}  {state.message.name}: decode error ({exc})"
        parts = []
        for sig in state.message.signals:
            if sig.name not in decoded:
                continue
            value = decoded[sig.name]
            if isinstance(value, float):
                value = f"{value:.{_decimals_for_scale(sig.scale)}f}"
            parts.append(f"{sig.name}={value}{(' ' + sig.unit) if sig.unit else ''}")
        return f"{line}  {state.message.name}: {', '.join(parts)}"

    def _run_async(self, work):
        """Run a blocking job on a worker thread; `busy` is true while it runs."""
        if self._busy:
            self.logMessage.emit("Another operation is still running")
            return

        def runner():
            try:
                work()
            finally:
                self._busy = False
                self.busyChanged.emit()

        self._busy = True
        self.busyChanged.emit()
        threading.Thread(target=runner, daemon=True).start()

    def _run_commands(self, commands: list[list[str]], tolerate: Optional[dict[int, str]] = None) -> bool:
        """Run commands in order; stop and log on the first failure.

        `tolerate` maps a command index to stderr text that counts as success.
        """
        for i, cmd in enumerate(commands):
            text = " ".join(cmd)
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            except FileNotFoundError:
                self.logMessage.emit(f"'{cmd[0]}' command not found")
                return False
            except Exception as exc:  # noqa: BLE001
                self.logMessage.emit(f"{text} failed: {exc}")
                return False
            if result.returncode != 0 and not (
                tolerate and i in tolerate and tolerate[i] in result.stderr
            ):
                self.logMessage.emit(f"{text} failed: {result.stderr.strip()}")
                return False
        return True

    def _reencode(self, msg_id: int, push_to_task: bool) -> Optional[bytes]:
        state = self._tx_states.get(msg_id)
        if state is None:
            return None
        try:
            data = state.message.encode(state.values, padding=True, strict=False)
        except Exception as exc:  # noqa: BLE001
            self.logMessage.emit(f"Encode error for {state.message.name}: {exc}")
            return None
        row = self._model.find_row_by_id(msg_id)
        if row >= 0:
            self._model.update_field(row, "raw_hex", data.hex(" ").upper(), MessageListModel.RawHexRole)
        if push_to_task and state.task is not None:
            can_msg = can.Message(
                arbitration_id=state.message.frame_id,
                data=data,
                is_extended_id=state.message.is_extended_frame,
            )
            try:
                state.task.modify_data(can_msg)
            except Exception as exc:  # noqa: BLE001
                self.logMessage.emit(f"Failed to update running TX for {state.message.name}: {exc}")
        return data

    def _refresh_raw_hex(self, row: int):
        msg_id = self._model._rows[row]["msg_id"]
        self._reencode(msg_id, push_to_task=False)

    def _stop_all_periodic(self):
        for state in self._tx_states.values():
            if state.task is not None:
                try:
                    state.task.stop()
                except Exception:  # noqa: BLE001
                    pass
                state.task = None
