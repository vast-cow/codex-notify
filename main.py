#!/usr/bin/env python3
"""Monitor a shared Codex app-server for completed turns and requests for action."""

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

from app_server import Settings, WebSocketClient
from monitor import Monitor
from notifications import Notifier, load_config


log = logging.getLogger("notify")


class MonitorFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        local_time = datetime.fromtimestamp(record.created, timezone.utc).astimezone()
        return local_time.isoformat(timespec="milliseconds")

    def format(self, record):
        message = super().format(record)
        for field in ("thread_id", "turn_id", "notification_type"):
            value = getattr(record, field, None)
            if isinstance(value, str) and value:
                message += f" {field}={value!r}"
        return message


def main():
    started_at = time.time()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true", help="log thread state and history checks")
    args = parser.parse_args()
    handler = logging.StreamHandler()
    handler.setFormatter(MonitorFormatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    log.setLevel(logging.DEBUG if args.debug else logging.INFO)

    try:
        config = load_config()
        notifier = Notifier(config)
        settings = Settings.from_config(config.get("app_server", {}))
        transport = WebSocketClient(settings)
    except (OSError, ValueError, TypeError, OverflowError, ImportError) as exc:
        log.error("Configuration error: %s", exc)
        return 1

    try:
        asyncio.run(Monitor(settings, transport, notifier, started_at).run())
    except KeyboardInterrupt:
        log.info("Monitoring stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
