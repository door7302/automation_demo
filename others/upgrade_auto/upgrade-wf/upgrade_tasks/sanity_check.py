"""Task: post-reboot sanity check based on chassis/system alarms."""

import asyncio

from temporalio import activity

from ._common import _connect
from .models import DeviceConnection


@activity.defn
async def sanity_check(conn: DeviceConnection) -> dict:
    """Returns {'ok': bool, 'hw_failure': bool, 'details': str} from device alarms."""

    def _run() -> dict:
        with _connect(conn) as dev:
            chassis = dev.show("show chassis alarms", fmt="text")
            system = dev.show("show system alarms", fmt="text")
        chassis_clear = "No alarms currently active" in chassis
        system_clear = "No alarms currently active" in system
        ok = chassis_clear and system_clear
        details_parts = []
        if not chassis_clear:
            details_parts.append("chassis alarms:\n" + chassis.strip())
        if not system_clear:
            details_parts.append("system alarms:\n" + system.strip())
        return {
            "ok": ok,
            "hw_failure": not chassis_clear,  # chassis alarms == hardware concern
            "details": "\n\n".join(details_parts) or "all alarms clear",
        }

    return await asyncio.to_thread(_run)
