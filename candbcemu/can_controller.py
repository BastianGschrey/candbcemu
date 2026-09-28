"""Qt/QML-facing controller that ties a DBC database to a live python-can bus."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import can
import cantools
from cantools.database import Message as DbcMessage
from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Property, Qt, Signal, Slot

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
    if not scale:
        return 0
    value = abs(scale)
    decimals = 0
    while value < 1 and decimals < 6:
        value *= 10
        decimals += 1
    return decimals


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
        self.endResetModel()

    def find_row_by_id(self, msg_id: int) -> int:
        for i, row in enumerate(self._rows):
            if row["msg_id"] == msg_id:
                return i
        return -1

    def update_field(self, row: int, field_name: str, value, role: int):
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = MessageListModel(self)
        self._db: Optional[cantools.database.Database] = None
        self._bus: Optional[can.BusABC] = None
        self._tx_states: dict[int, TxState] = {}
        self._connected = False
        self._dbc_name = ""

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
            for sig in msg.signals:
                minimum, maximum = _effective_range(sig)
                initial = sig.initial if sig.initial is not None else (
                    0.0 if minimum <= 0.0 <= maximum else minimum
                )
                values[sig.name] = initial
                choices = []
                if sig.choices:
                    choices = [
                        {"value": int(raw), "name": str(name)}
                        for raw, name in sorted(sig.choices.items())
                    ]
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
                "msg_id": msg.frame_id,
                "msg_id_hex": f"0x{msg.frame_id:X}",
                "dlc": msg.length,
                "comment": msg.comment or "",
                "cycle_time": msg.cycle_time or 100,
                "transmit_enabled": False,
                "raw_hex": "",
                "sig_list": sig_list,
                "extended": bool(msg.is_extended_frame),
            })
            self._tx_states[msg.frame_id] = TxState(message=msg, values=values)

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
        self._connected = True
        self.connectionChanged.emit(True)
        self.logMessage.emit(f"Connected: {iface_type} channel='{channel}' bitrate={bitrate}")
        return True

    @Slot()
    def disconnectBus(self):
        self._stop_all_periodic()
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

    @Slot(str, int, result=bool)
    def applySocketcanBitrate(self, channel: str, bitrate: int) -> bool:
        """Bring a SocketCAN link down, set its bitrate, and bring it back up.

        Requires CAP_NET_ADMIN (typically root) - reports failure via logMessage
        rather than silently retrying with elevated privileges.
        """
        commands = [
            ["ip", "link", "set", channel, "down"],
            ["ip", "link", "set", channel, "type", "can", "bitrate", str(bitrate)],
            ["ip", "link", "set", channel, "up"],
        ]
        for cmd in commands:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            except FileNotFoundError:
                self.logMessage.emit("'ip' command not found - install iproute2")
                return False
            except Exception as exc:  # noqa: BLE001
                self.logMessage.emit(f"{' '.join(cmd)} failed: {exc}")
                return False
            if result.returncode != 0:
                self.logMessage.emit(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
                return False
        self.logMessage.emit(f"{channel} configured at {bitrate} bps")
        self.availableInterfacesChanged.emit()
        return True

    @Slot(str, result=bool)
    def createVcanInterface(self, name: str) -> bool:
        """Load the vcan kernel module and bring up a virtual SocketCAN link.

        Equivalent to scripts/setup_vcan.sh - requires CAP_NET_ADMIN (root).
        Safe to call on an interface that already exists.
        """
        name = name.strip() or "vcan0"

        try:
            result = subprocess.run(
                ["modprobe", "vcan"], capture_output=True, text=True, timeout=5
            )
        except FileNotFoundError:
            self.logMessage.emit("'modprobe' command not found")
            return False
        except Exception as exc:  # noqa: BLE001
            self.logMessage.emit(f"modprobe vcan failed: {exc}")
            return False
        if result.returncode != 0:
            self.logMessage.emit(f"modprobe vcan failed: {result.stderr.strip()}")
            return False

        try:
            result = subprocess.run(
                ["ip", "link", "add", "dev", name, "type", "vcan"],
                capture_output=True, text=True, timeout=5,
            )
        except FileNotFoundError:
            self.logMessage.emit("'ip' command not found - install iproute2")
            return False
        except Exception as exc:  # noqa: BLE001
            self.logMessage.emit(f"ip link add {name} failed: {exc}")
            return False
        if result.returncode != 0 and "File exists" not in result.stderr:
            self.logMessage.emit(f"ip link add {name} failed: {result.stderr.strip()}")
            return False

        try:
            result = subprocess.run(
                ["ip", "link", "set", "up", name],
                capture_output=True, text=True, timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            self.logMessage.emit(f"ip link set up {name} failed: {exc}")
            return False
        if result.returncode != 0:
            self.logMessage.emit(f"ip link set up {name} failed: {result.stderr.strip()}")
            return False

        self.logMessage.emit(f"Virtual CAN interface '{name}' is up")
        self.availableInterfacesChanged.emit()
        return True

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
                arbitration_id=msg_id,
                data=data,
                is_extended_id=state.message.is_extended_frame,
            )
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
            arbitration_id=msg_id,
            data=data,
            is_extended_id=state.message.is_extended_frame,
        )
        try:
            self._bus.send(can_msg)
            self.logMessage.emit(f"Sent {state.message.name}: {data.hex(' ').upper()}")
        except Exception as exc:  # noqa: BLE001
            self.logMessage.emit(f"Send failed for {state.message.name}: {exc}")

    # -- internals -------------------------------------------------------
    def _reencode(self, msg_id: int, push_to_task: bool) -> Optional[bytes]:
        state = self._tx_states.get(msg_id)
        if state is None or self._db is None:
            return None
        try:
            data = self._db.encode_message(msg_id, state.values, padding=True, strict=False)
        except Exception as exc:  # noqa: BLE001
            self.logMessage.emit(f"Encode error for 0x{msg_id:X}: {exc}")
            return None
        row = self._model.find_row_by_id(msg_id)
        if row >= 0:
            self._model.update_field(row, "raw_hex", data.hex(" ").upper(), MessageListModel.RawHexRole)
        if push_to_task and state.task is not None:
            can_msg = can.Message(
                arbitration_id=msg_id,
                data=data,
                is_extended_id=state.message.is_extended_frame,
            )
            try:
                state.task.modify_data(can_msg)
            except Exception as exc:  # noqa: BLE001
                self.logMessage.emit(f"Failed to update running TX for 0x{msg_id:X}: {exc}")
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
