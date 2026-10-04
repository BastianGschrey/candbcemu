"""Compares the ESP32 firmware core (dbc.cpp, built for the PC) with cantools and the Python sender:
- frame bytes for min / max / mid values of every signal,
- the value ranges.
Run from the repository root:  .venv/bin/python esp32/test/compare.py"""
import subprocess
import sys
from pathlib import Path

import cantools

ROOT = Path(__file__).resolve().parents[2]
BIN = Path("/tmp/claude-1000/host_encode")
sys.path.insert(0, str(ROOT))
from candbcemu.headless.patterns import signal_range  # noqa: E402


def build():
    subprocess.run(["g++", "-std=c++17", "-O1", "-o", str(BIN), str(ROOT / "esp32/test/host_encode.cpp"),
                    str(ROOT / "esp32/src/dbc.cpp")], check=True)


def run(dbc, mode):
    out = subprocess.run([str(BIN), str(dbc), mode], capture_output=True, text=True, check=True).stdout
    return [l.split() for l in out.strip().splitlines()]


def main():
    build()
    bad = 0
    for dbc in sorted((ROOT / "databases").glob("*.dbc")):
        db = cantools.database.load_file(str(dbc))
        # ranges
        for line in run(dbc, "info"):
            msg = db.get_message_by_frame_id(int(line[0], 16))
            assert len(msg.signals) == len(line) - 4, (msg.name, len(msg.signals), len(line) - 4)
            by_name = {x.name: x for x in msg.signals}
            for item in line[4:]:
                name, lo, hi = item.rsplit(":", 2)
                sig = by_name[name]
                plo, phi = signal_range(sig)
                if abs(float(lo) - plo) > 1e-3 * max(1, abs(plo)) or abs(float(hi) - phi) > 1e-3 * max(1, abs(phi)):
                    print(f"RANGE {dbc.name} {msg.name}.{sig.name}: C++ {lo}..{hi} python {plo}..{phi}")
                    bad += 1
        # frames
        for mode in ("min", "max", "mid"):
            for line in run(dbc, mode):
                msg = db.get_message_by_frame_id(int(line[0], 16))
                values = {}
                for sig in msg.signals:
                    lo, hi = signal_range(sig)
                    values[sig.name] = lo if mode == "min" else hi if mode == "max" else (lo + hi) / 2
                want = msg.encode(values, padding=False, strict=False).hex(" ").upper().split()
                got = line[1:]
                if want[:len(got)] != got:
                    print(f"FRAME {dbc.name} {msg.name} {mode}: C++ {got} cantools {want}")
                    bad += 1
    print("OK" if not bad else f"{bad} mismatches")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
