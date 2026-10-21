# CAN DBC Emulator

A desktop GUI (Python + PySide6/QML) for sending arbitrary CAN traffic defined by a
DBC database. Pick a `.dbc` file, pick a CAN interface and bitrate, then drive each
signal with a slider (or dropdown for enumerated signals) and transmit it - either
once or periodically at the message's cycle time.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Running

```bash
./.venv/bin/python main.py                       # start empty
./.venv/bin/python main.py databases/emu_black.dbc  # auto-load a DBC on startup
```

Or use the launcher script from anywhere:

```bash
./candbcemu.sh [optional: path/to.dbc]
```

## Using it

1. **Load DBC…** - pick a `.dbc` file (two samples are in `databases/`).
2. **Interface** - `socketcan` (real or virtual Linux CAN interface), `virtual`
   (python-can's in-process bus, no hardware/root needed - good for testing),
   or `slcan` (serial/USB CAN adapters such as CANable/candleLight running slcan
   firmware; channel is the serial device, e.g. `/dev/ttyACM0`).
3. **Channel** - pick from the detected SocketCAN interfaces or type your own
   (e.g. `can0`, `vcan0`, `/dev/ttyACM0`).
4. **Bitrate** - only meaningful for `socketcan` (see below) and `slcan`.
5. **Connect**.
6. Expand a message to reveal its signals. Each numeric signal gets a slider
   scaled to its DBC-defined range/resolution; enumerated signals (DBC value
   tables) get a dropdown instead. The raw encoded bytes are shown live per
   message.
7. Flip a message's **TX** switch to transmit it periodically at its cycle
   time (editable, defaults to 100 ms if the DBC doesn't specify one), or hit
   **Send once** for a single frame.

8. Every frame received on the bus is printed to the **Log** panel (`RX 0x360 [8] …`).
   If the ID is in the loaded DBC, the signals are decoded as well. Untick
   **Show received frames** in the log header to mute it; on a very busy bus,
   excess frames are dropped from the log and reported as `RX overflow`.

9. **Simulation** (row under the connection settings): *Komplett*, *Start*, *Fahren* and
   *Konsistent* drive all signals with the same endless engine simulation as the headless
   sender (see below); *Aus* switches it off. Moving a slider by hand pins that signal until
   a mode is picked again.

### SocketCAN bitrate

On Linux, a SocketCAN interface's bitrate is a property of the network link
itself, not something an application sets per-connection. The **Apply
Bitrate** button runs:

```
ip link set <iface> down
ip link set <iface> type can bitrate <rate>
ip link set <iface> up
```

This needs `CAP_NET_ADMIN` (root). If it fails, either run the app with
sufficient privileges or configure the interface yourself beforehand.

### Testing without real hardware

With interface type `socketcan`, the **Create vcan** button next to the
channel field loads the `vcan` kernel module and brings up a virtual
SocketCAN link with that name (default `vcan0`) - the in-app equivalent of:

```bash
sudo ./scripts/setup_vcan.sh vcan0
```

Both need `CAP_NET_ADMIN` (root); if the button's log message reports a
permission error, run the app itself as root or run the script beforehand
instead.

Once the interface exists, select channel `vcan0` and connect (no bitrate
configuration needed for virtual interfaces). Watch traffic with `candump
vcan0` in another terminal. This is also how two separate applications on
the same machine (e.g. this sender and a separate receiver tool) talk to
each other - both just open the same `vcan0` interface.

Alternatively, interface type `virtual` needs no setup at all, but only
works *within the same Python process* - it's meant for quick UI testing,
not for talking to other tools or processes.

## Project layout

- `main.py` - application entry point
- `candbcemu/can_controller.py` - QML-facing controller: DBC loading, signal
  encoding (via `cantools`), bus connection and periodic transmission (via
  `python-can`)
- `candbcemu/net_utils.py` - SocketCAN interface discovery
- `qml/main.qml` - the UI
- `databases/` - sample DBC files
- `scripts/setup_vcan.sh` - helper to create a virtual CAN interface
- `candbcemu.sh` - convenience launcher

## Headless sender (Raspberry Pi, no screen)

`candbcemu.headless` sends demo data for a DBC without Qt or a display, e.g. from a
Raspberry Pi 2B with a PiCAN2 as a test source for another CAN device. It needs only
`cantools` and `python-can`.

Instead of plain sine waves, the signals of an engine follow one shared, endless vehicle
simulation: cold start (cranking, fast idle), idle, pulling away, shifting through six
gears, cruising, braking to a stop - and again, with random speeds and driving style. The
values stay consistent: rpm follows speed and gear, throttle/load/boost follow the pedal,
coolant and oil warm up slowly, lambda goes lean in overrun and rich under load, the
brake/clutch/fan/decel-cut flags switch when they should. Signals are assigned by their
name and unit (rpm, speed, gear, throttle/pedal, load, MAP/boost, coolant/oil/air/EGT
temperature, oil/fuel pressure, lambda/AFR, battery voltage, ignition angle, injector duty,
wheel speeds, ...); the simulated value is scaled into the signal's physical range
(°C, Kelvin, kPa, bar, mph are recognised). All other signals get a curve from their range
and name/unit (sine/triangle/square), enumerations step through their values, multiplexed
messages cycle through their selector values. Values are limited to what the DBC range *and*
the signal's bits can hold. The simulation restarts (cold engine) whenever sending starts. Four buttons in the web UI
(`POST /api/mode`) choose the scenario: **Komplett** (cold start, then endless driving),
**Start** (cold start, then idle only - the engine warms up), **Fahren** (warm engine, driving
right away) and **Konsistent** (warm idle that never changes: rpm, lambda, voltage ... fluctuate
slightly like a real idle).

```bash
python3 -m candbcemu.headless --dbc emu_black.dbc --channel can0 --bitrate 500000 --auto
# test without hardware:   sudo ./scripts/setup_vcan.sh vcan0
python3 -m candbcemu.headless --dbc emu_black.dbc --channel vcan0 --no-set-bitrate --auto
```

Web UI on `http://<host>:8080/` (no login - test device on a trusted network): choose the
DBC, start/stop, switch messages on/off, change cycle times, and per signal either "Auto"
(demo curve) or "Fest" with a slider. `--config sender.json` overrides curves per signal:

```json
{"signals": {"RPM": {"kind": "triangle", "period": 6, "lo": 900, "hi": 7000}},
 "messages": {"ID_0x360": {"cycle": 20, "enabled": true}}}
```

(`kind`: engine, sine, triangle, square, const, choices, manual - `engine` only makes sense for signals
the simulation already drives; give any other signal a curve instead.)

### Raspberry Pi 2B + PiCAN2

1. Flash Raspberry Pi OS Lite (32-bit) with the Raspberry Pi Imager (enable SSH, set Wi-Fi/user there).
2. Copy this repository to the Pi, then on the Pi: `sudo ./scripts/pi-install.sh emu_black.dbc`
   (installs the venv, adds `dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25` to `config.txt`,
   enables the `candbcemu-headless` service), then `sudo reboot`.
3. Wire CAN-H, CAN-L and GND to the device under test. **Terminate with 120 Ω at both ends of the
   bus** (PiCAN2 has a solder jumper for its 120 Ω) - without a second node and termination the
   controller reports errors continuously.
4. Check: `ip -details link show can0`, `candump can0`, web UI on port 8080.

Tests: `python3 -m unittest discover -s tests`.
