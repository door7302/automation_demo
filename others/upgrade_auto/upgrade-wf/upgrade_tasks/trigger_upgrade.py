"""Task: validate and install the already-uploaded image package."""

import asyncio

from temporalio import activity

from ._common import _connect
from .models import DeviceConnection


@activity.defn
async def trigger_upgrade(
    conn: DeviceConnection, package: str, remote_path: str
) -> str:
    """Validate and install the (already uploaded) package. Returns the SW message."""

    def _run() -> str:
        with _connect(conn) as dev:
            _status, message = dev.upgrade(
                package=package,
                remote_path=remote_path,
                validate=True,
                no_copy=True,   # image was already uploaded
                reboot=False,   # reboot is a separate, approved step
            )
        return message

    return await asyncio.to_thread(_run)
