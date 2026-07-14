"""Shared helpers for the device-upgrade activities.

Every activity opens a NETCONF session via ``juniper_api.JuniperDevice``.
JuniperDevice (PyEZ/NETCONF) is synchronous and blocking, so each activity runs
the device work inside a worker thread via ``asyncio.to_thread`` to keep the
asyncio event loop responsive.
"""

import logging

from juniper_api import JuniperDevice
from juniper_api.exceptions import JuniperDeviceError  # re-exported for tasks

from .models import DeviceConnection

logger = logging.getLogger("device_upgrade")

# Operational state captured for the pre/post upgrade comparison.
SNAPSHOT_COMMANDS = (
    "show version",
    "show chassis hardware",
    "show interfaces terse",
    "show bgp summary",
    "show route summary",
    "show system alarms",
    "show chassis alarms",
)


def _connect(conn: DeviceConnection, gather_facts: bool = True) -> JuniperDevice:
    """Build (not yet open) a JuniperDevice from the connection params."""
    return JuniperDevice(
        host=conn.host,
        user=conn.user,
        passwd=conn.passwd,
        port=conn.port,
        ssh_private_key_file=conn.ssh_private_key_file,
        gather_facts=gather_facts,
        logger=logger,
    )


__all__ = ["JuniperDeviceError", "SNAPSHOT_COMMANDS", "_connect", "logger"]
