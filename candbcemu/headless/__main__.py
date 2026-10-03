"""python3 -m candbcemu.headless --dbc databases/emu_black.dbc --channel can0 --auto"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from .sender import Sender
from .web import make_server


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Headless CAN demo sender")
    ap.add_argument("--dbc", help="DBC file to load (name inside --dbc-dir or a path)")
    ap.add_argument("--dbc-dir", default=str(root / "databases"), help="folder the web UI lists DBCs from")
    ap.add_argument("--interface", default="socketcan", choices=["socketcan", "virtual", "slcan"])
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--bitrate", type=int, default=500000)
    ap.add_argument("--no-set-bitrate", action="store_true", help="do not run 'ip link set ... bitrate'")
    ap.add_argument("--auto", action="store_true", help="connect and send all messages right away")
    ap.add_argument("--config", help="JSON file with per-signal/-message overrides")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args(argv)

    sender = Sender(args.config)
    if args.dbc:
        path = Path(args.dbc)
        if not path.is_file():
            path = Path(args.dbc_dir) / args.dbc
        sender.load_dbc(str(path))
    if args.auto:
        if args.dbc is None:
            ap.error("--auto needs --dbc")
        sender.set_all_enabled(True)
        sender.start(args.interface, args.channel, args.bitrate, not args.no_set_bitrate)

    server = None
    if not args.no_web:
        server = make_server(sender, Path(args.dbc_dir), args.port, args.host)
        print(f"web UI on http://{args.host}:{args.port}/", flush=True)

    def shutdown(*_):
        if server is not None:
            # shutdown() must not run on the thread inside serve_forever()
            import threading
            threading.Thread(target=server.shutdown, daemon=True).start()
        else:
            raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        if server is not None:
            server.serve_forever()
        else:
            signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        sender.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
