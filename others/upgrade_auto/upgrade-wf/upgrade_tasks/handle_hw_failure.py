"""Task: compensation handler for a detected hardware failure."""

import asyncio

from temporalio import activity

from ._common import _connect, logger
from .models import DeviceConnection


@activity.defn
async def handle_hw_failure(conn: DeviceConnection, details: str) -> None:
    def _run() -> None:
        with _connect(conn) as dev:
            hardware = dev.show("show chassis hardware", fmt="text")
        logger.error("HW failure on %s: %s\n%s", conn.host, details, hardware)

    await asyncio.to_thread(_run)
