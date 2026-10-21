import json
import threading
import unittest
import urllib.request
from pathlib import Path

import can
import cantools

from candbcemu.headless.patterns import Engine, evaluate, profile_for, signal_range
from candbcemu.headless.sender import Sender
from candbcemu.headless.web import make_server

ROOT = Path(__file__).resolve().parents[1]
DBCS = sorted((ROOT / "databases").glob("*.dbc"))


class PatternTests(unittest.TestCase):
    def test_values_stay_inside_range_and_encode(self):
        for path in DBCS:
            db = cantools.database.load_file(str(path))
            for msg in db.messages:
                for sig in msg.signals:
                    lo, hi = signal_range(sig)
                    profile = profile_for(sig, sim=Engine())
                    for t in (0.0, 1.3, 7.7, 33.0, 123.4, 800.0):
                        v = evaluate(profile, t)
                        self.assertTrue(lo - 1e-6 <= v <= hi + 1e-6 or profile.kind == "choices",
                                        f"{path.name} {msg.name}.{sig.name}={v} not in {lo}..{hi}")


class EngineTests(unittest.TestCase):
    def test_drives_endlessly_and_consistently(self):
        sim = Engine()
        phases, gears, rpms, speeds = set(), set(), [], []
        for i in range(0, 3 * 3600 * 2):                       # three hours in half-second steps
            t = i * 0.5
            sim.advance(t)
            phases.add(sim.phase)
            gears.add(sim.gear)
            rpms.append(sim.rpm)
            speeds.append(sim.v)
            self.assertLessEqual(sim.rpm, 7300)
            if sim.running and not sim.shift_left:
                self.assertGreaterEqual(sim.rpm, 600)
        self.assertEqual(phases, {"idle", "accel", "cruise", "decel"})
        self.assertGreaterEqual(len(gears), 5)
        self.assertGreater(max(speeds), 100)
        self.assertLess(min(speeds), 1)                         # comes to a stop again and again
        self.assertGreater(sum(1 for v in speeds if v < 1), 100)
        self.assertGreater(sim.coolant, 85)                     # warm after a while ...
        self.assertLess(sim.coolant, 110)                       # ... and held there

    def test_modes(self):
        sim = Engine()
        for mode in Engine.MODES:
            sim.set_mode(mode, 1000.0)
            speeds, rpms = [], []
            for i in range(0, 1800):                           # 15 minutes
                sim.advance(1000.0 + i * 0.5)
                speeds.append(sim.v)
                rpms.append(sim.rpm)
            if mode in ("start", "idle"):
                self.assertEqual(max(speeds), 0.0, mode)       # never drives off
            else:
                self.assertGreater(max(speeds), 30, mode)
            if mode == "idle":
                self.assertGreater(sim.coolant, 88)            # warm from the beginning ...
                tail = rpms[100:]
                self.assertGreater(max(tail) - min(tail), 20)  # ... and the idle hunts a little
                self.assertLess(max(tail) - min(tail), 250)
            if mode == "start":
                warming = Engine()
                warming.set_mode("start", 0.0)
                self.assertLess(warming.get("coolant", 10.0), warming.get("coolant", 200.0))   # warming up
        self.assertFalse(sim.set_mode("nonsense"))

    def test_cold_start_and_time_reset(self):
        sim = Engine()
        self.assertEqual(sim.get("rpm", 0.0), 0.0)
        self.assertGreater(sim.get("rpm", 3.0), 0.0)
        self.assertLess(sim.get("coolant", 3.0), 0.05)          # still cold
        sim.get("coolant", 500.0)
        self.assertEqual(sim.get("rpm", 0.0), 0.0)              # sender restarted: starts over

    def test_names_are_matched_to_roles(self):
        db = cantools.database.load_file(str(ROOT / "databases" / "emu_black.dbc"))
        sim = Engine()
        roles = {s.name: profile_for(s, sim=sim).role for m in db.messages for s in m.signals}
        for name, role in (("RPM", "rpm"), ("VSPD", "speed"), ("GEAR", "gear"), ("CLT", "coolant"),
                           ("TPS", "throttle"), ("LAMBDA", "lambda"), ("BATT", "volt"), ("AIN1", "")):
            self.assertEqual(roles[name], role, name)


class GearTests(unittest.TestCase):
    def test_gear_steps_through_whole_gears(self):
        db = cantools.database.load_file(str(ROOT / "databases" / "emu_black.dbc"))
        sig = next(s for m in db.messages for s in m.signals if s.name == "GEAR")
        p = profile_for(sig, sim=Engine())
        values = [evaluate(p, t * 0.25) for t in range(0, 4 * 600)]
        self.assertTrue(all(v == int(v) for v in values))
        self.assertGreaterEqual(len(set(values)), 5)          # climbs through several gears ...
        changes = sum(1 for a, b in zip(values, values[1:]) if a != b)
        self.assertLess(changes, len(values) // 20)           # ... and does not flip back and forth


class SenderTests(unittest.TestCase):
    def test_every_message_of_every_dbc_is_sent_and_decodes(self):
        for path in DBCS:
            sender = Sender()
            sender.load_dbc(str(path))
            sender.set_all_enabled(True)
            channel = f"test-{path.stem}"
            sender.start("virtual", channel, 500000)
            listener = can.Bus(channel=channel, interface="virtual", receive_own_messages=False)
            try:
                db = cantools.database.load_file(str(path))
                seen = set()
                import time
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline and len(seen) < len(db.messages):
                    frame = listener.recv(timeout=0.2)
                    if frame is not None:
                        db.decode_message(frame.arbitration_id, frame.data, decode_choices=False,
                                          allow_truncated=True)
                        seen.add(frame.arbitration_id)
                snap = sender.snapshot()
                self.assertEqual([m["name"] for m in snap["messages"] if m["errors"]], [],
                                 [m["lastError"] for m in snap["messages"] if m["errors"]])
                self.assertEqual(seen, {m.frame_id for m in db.messages}, path.name)
            finally:
                listener.shutdown()
                sender.stop()

    def test_manual_override_and_message_switch(self):
        sender = Sender()
        sender.load_dbc(str(DBCS[0]))
        snap = sender.snapshot()
        msg = snap["messages"][0]
        sig = next(s for s in msg["signals"] if not s["selector"])
        self.assertTrue(sender.set_signal(msg["key"], sig["name"], kind="manual", value=sig["hi"] + 1e9))
        s = next(x for x in sender.snapshot()["messages"][0]["signals"] if x["name"] == sig["name"])
        self.assertEqual((s["kind"], s["manual"]), ("manual", sig["hi"]))   # clamped to the DBC range
        sender.set_signal(msg["key"], sig["name"], kind="auto")
        s = next(x for x in sender.snapshot()["messages"][0]["signals"] if x["name"] == sig["name"])
        self.assertNotEqual(s["kind"], "manual")
        self.assertTrue(sender.set_message(msg["key"], enabled=True, cycle_ms=1))
        self.assertEqual(sender.snapshot()["messages"][0]["cycle"], 5)       # lower bound


    def test_auto_min_max(self):
        sender = Sender()
        sender.load_dbc(str(DBCS[0]))
        msg = sender.snapshot()["messages"][0]
        sig = next(s for s in msg["signals"] if not s["selector"] and s["kind"] in ("engine", "sine", "triangle"))
        k, n = msg["key"], sig["name"]
        get = lambda: next(x for x in sender.snapshot()["messages"][0]["signals"] if x["name"] == n)
        span = sig["hi"] - sig["lo"]
        sender.set_signal(k, n, lo=sig["lo"] + 0.2 * span, hi=sig["lo"] + 0.4 * span)
        self.assertAlmostEqual(get()["alo"], sig["lo"] + 0.2 * span)
        self.assertAlmostEqual(get()["ahi"], sig["lo"] + 0.4 * span)
        sender.set_signal(k, n, hi=sig["hi"] + 1e9)                 # clamped to what the signal can hold
        self.assertEqual(get()["ahi"], sig["hi"])
        sender.set_signal(k, n, lo=sig["hi"], hi=sig["lo"])         # inverted: hi never below lo
        self.assertGreaterEqual(get()["ahi"], get()["alo"])
        sender.set_signal(k, n, reset=True)
        self.assertNotEqual(get()["alo"], sig["hi"])
        self.assertLess(get()["alo"], get()["ahi"])


class WebTests(unittest.TestCase):
    def test_api(self):
        sender = Sender()
        server = make_server(sender, ROOT / "databases", 0, "127.0.0.1")
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def call(path, body=None):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                             data=None if body is None else json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())
            status, state = call("/api/state")
            self.assertEqual(status, 200)
            self.assertIn(DBCS[0].name, state["dbcs"])
            self.assertEqual(call("/api/dbc", {"name": "../x.dbc"})[0], 400)
            self.assertEqual(call("/api/dbc", {"name": DBCS[0].name})[0], 200)
            state = call("/api/state")[1]
            self.assertGreater(len(state["messages"]), 0)
            key = state["messages"][0]["key"]
            self.assertEqual(call("/api/message", {"key": key, "enabled": True})[0], 200)
            self.assertTrue(call("/api/state")[1]["messages"][0]["enabled"])
            self.assertEqual(call("/api/message", {"key": -5, "enabled": True})[0], 404)
            self.assertEqual(call("/")[0] if False else urllib.request.urlopen(f"http://127.0.0.1:{port}/").status, 200)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
