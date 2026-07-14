"""Task: drain traffic away from the device before the upgrade."""

import asyncio
from typing import List

from temporalio import activity

from ._common import _connect
from .models import DeviceConnection


@activity.defn
async def drain_traffic(conn: DeviceConnection, payload: List[str]) -> None:
    def _run() -> None:
        with _connect(conn) as dev:
            dev.edit_config(
                payload="\n".join(payload),
                fmt="set",
                comment="drain traffic (upgrade workflow)",
            )

    await asyncio.to_thread(_run)
