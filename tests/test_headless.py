import json
import threading
import unittest
import urllib.request
from pathlib import Path

import can
import cantools

from candbcemu.headless.patterns import evaluate, profile_for, signal_range
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
                    profile = profile_for(sig)
                    for t in (0.0, 1.3, 7.7, 33.0, 123.4):
                        v = evaluate(profile, t)
                        self.assertTrue(lo - 1e-6 <= v <= hi + 1e-6 or profile.kind == "choices",
                                        f"{path.name} {msg.name}.{sig.name}={v} not in {lo}..{hi}")


class GearTests(unittest.TestCase):
    def test_gear_steps_through_whole_gears(self):
        db = cantools.database.load_file(str(ROOT / "databases" / "emu_black.dbc"))
        sig = next(s for m in db.messages for s in m.signals if s.name == "GEAR")
        p = profile_for(sig)
        values = [evaluate(p, t * 0.25) for t in range(0, 4 * 36)]
        self.assertTrue(all(v == int(v) for v in values))
        self.assertGreaterEqual(len(set(values)), 5)          # climbs through several gears ...
        changes = sum(1 for a, b in zip(values, values[1:]) if a != b)
        self.assertLess(changes, 20)                          # ... and does not flip back and forth


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
        sig = next(s for s in msg["signals"] if not s["selector"] and s["kind"] in ("sine", "triangle"))
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
