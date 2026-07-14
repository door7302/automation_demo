"""Task: reboot both routing engines of the device.

A vmhost image requires a vmhost reboot (``request vmhost reboot``) instead of a
plain RE reboot. This is auto-detected from the image name, mirroring
``juniper_api.JuniperDevice.upgrade()``.
"""

import asyncio
import os

from temporalio import activity

from ._common import _connect
from .models import DeviceConnection


@activity.defn
async def reboot_device(conn: DeviceConnection, image_path: str = "") -> str:
    vmhost = "vmhost" in os.path.basename(image_path).lower()

    def _run() -> str:
        with _connect(conn) as dev:
            return dev.reboot(
                target="re", routing_engine="both", vmhost=vmhost
            )

    return await asyncio.to_thread(_run)
