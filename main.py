#!/usr/bin/env python3
"""Monitor a shared Codex app-server for completed turns and requests for action."""

import argparse
import asyncio
import logging
import sys
import time

from app_server import Settings, WebSocketClient
from monitor import Monitor
from notifications import Notifier, load_config


def main():
    started_at = time.time()
    argparse.ArgumentParser(description=__doc__).parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s %(message)s")

    try:
        config = load_config()
        notifier = Notifier(config)
        settings = Settings.from_config(config.get("app_server", {}))
        transport = WebSocketClient(settings)
    except (OSError, ValueError, TypeError, OverflowError, ImportError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        asyncio.run(Monitor(settings, transport, notifier, started_at).run())
    except KeyboardInterrupt:
        logging.getLogger("notify").info("Monitoring stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
