# ESP32 CAN demo sender (MCP2515)

Same idea as `candbcemu.headless`, but on an ESP32 with an MCP2515 module: upload a `.dbc` in the
browser, the firmware sends demo data for every message (curves chosen per signal from its range,
name and unit), with per-signal override sliders.

## Wiring

| MCP2515 module | ESP32 |
|---|---|
| VCC | 5V (VIN) |
| GND | GND |
| CS | GPIO 5 |
| SCK | GPIO 18 |
| SI (MOSI) | GPIO 23 |
| SO (MISO) | GPIO 19 |
| INT | not used |

CAN_H / CAN_L to the bus, 120 Ω at both ends (jumper on the module).

## Build + flash (PlatformIO)

```bash
cd esp32
pio run -t upload --upload-port /dev/ttyUSB0
```

## Use

1. After the first flash the ESP32 opens the access point **CAN-Sender-ESP** (password `cansender`),
   page `http://192.168.4.1/`. Press **WLAN**, pick the network, enter the password: it restarts and
   joins it (`http://can-esp.local/` or the IP shown in the serial monitor).
2. **DBC hochladen** (kept in flash, several files possible), choose bit rate and the crystal of the
   module (8 or 16 MHz), **Starten**, switch messages on (**Alle an**).
3. Per signal: "Auto" (demo curve, with editable min/max - kept in flash per DBC and restored after a restart, ↺ resets) or "Fest" with a slider.

The status line shows the controller's TEC/REC error counters: if they climb, nobody acknowledges
the frames (wiring, termination, bit rate).

## Updating the firmware

After the first USB flash the web UI can replace the firmware itself: build (`pio run`), then press
**Firmware** in the header and pick `esp32/.pio/build/esp32dev/firmware.bin`. The device restarts
(a few seconds). Without a browser: `pio run -e ota -t upload` (password `cansender`).

## Tests (on the PC)

`dbc.cpp` is plain C++ and is compared with cantools (frame bytes for min/max/mid values and the
value ranges, for every DBC in `databases/`):

```bash
.venv/bin/python esp32/test/compare.py
```
