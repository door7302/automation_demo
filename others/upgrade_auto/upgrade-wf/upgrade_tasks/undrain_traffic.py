"""Task: undrain (restore) traffic to the device after the upgrade."""

import asyncio
from typing import List

from temporalio import activity

from ._common import _connect
from .models import DeviceConnection


@activity.defn
async def undrain_traffic(conn: DeviceConnection, payload: List[str]) -> None:
    def _run() -> None:
        with _connect(conn) as dev:
            dev.edit_config(
                payload="\n".join(payload),
                fmt="set",
                comment="undrain traffic (upgrade workflow)",
            )

    await asyncio.to_thread(_run)
