"""Task: compensation handler for a bad post-upgrade device state."""

import asyncio

from temporalio import activity

from ._common import _connect, logger
from .models import DeviceConnection


@activity.defn
async def handle_bad_state(conn: DeviceConnection, details: str) -> None:
    def _run() -> None:
        with _connect(conn) as dev:
            diag = dev.show("show chassis routing-engine", fmt="text")
        logger.error("Bad state on %s: %s\n%s", conn.host, details, diag)

    await asyncio.to_thread(_run)
