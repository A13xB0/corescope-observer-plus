from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .app import App

LOG = logging.getLogger("openhop.corescope_observer_plus")
TICK_SECONDS = 5


def main() -> int:
    parser = argparse.ArgumentParser(prog="openhop-corescope-observer-plus")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--once", action="store_true", help="Run one tick and exit.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, os.environ.get("OPENHOP_OBSERVER_PLUS_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    data_dir = Path(os.environ.get("OPENHOP_PLUGIN_DATA") or Path.cwd()).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    app = App(data_dir)
    LOG.info("starting version=%s data_dir=%s", __version__, data_dir)
    while not stop.is_set():
        try:
            app.tick()
        except Exception:  # noqa: BLE001 - keep running; the next tick retries
            LOG.exception("tick failed")
        if args.once:
            break
        stop.wait(TICK_SECONDS)
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
