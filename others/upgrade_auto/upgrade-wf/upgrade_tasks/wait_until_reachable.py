"""Task: poll until the device is reachable again after a reboot."""

import asyncio
from datetime import timedelta

from temporalio import activity
from temporalio.exceptions import ApplicationError

from ._common import JuniperDeviceError, _connect
from .models import DeviceConnection


@activity.defn
async def wait_until_reachable(conn: DeviceConnection) -> None:
    """Poll until the device accepts NETCONF and answers a show, max 30 minutes."""

    def _probe() -> bool:
        dev = _connect(conn, gather_facts=False)
        try:
            dev.open()
            dev.show("show system uptime", fmt="text")
            return True
        except JuniperDeviceError:
            return False
        finally:
            dev.close()

    poll_interval = 30  # seconds
    elapsed = 0
    max_seconds = int(timedelta(minutes=30).total_seconds())

    while elapsed < max_seconds:
        activity.heartbeat()
        if await asyncio.to_thread(_probe):
            return
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise ApplicationError(
        f"Device {conn.host} not reachable after 30 minutes",
        non_retryable=True,
    )
