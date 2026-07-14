"""Task: collect device facts via ``show version``."""

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from ._common import _connect
from .models import DeviceConnection, DeviceFacts


@activity.defn
async def collect_facts(conn: DeviceConnection) -> DeviceFacts:
    """Collect device facts via 'show version'. Raises on failure."""

    def _run() -> DeviceFacts:
        with _connect(conn) as dev:
            ver_xml = dev.show("show version", fmt="xml")
            # Pull clean values straight from the XML — no XPath by hand.
            model = (
                dev.select(ver_xml, "product-model", first=True)
                or dev.select(ver_xml, "product-name", first=True)
            )
            hostname = dev.select(ver_xml, "host-name", first=True)
            sw_version = dev.select(ver_xml, "junos-version", first=True)

        if not model:
            raise ApplicationError(
                f"Failed to retrieve model from {conn.host}", non_retryable=True
            )
        return DeviceFacts(
            hostname=hostname or conn.host,
            model=model,
            sw_version=sw_version or "unknown",
            reachable=True,
        )

    return await asyncio.to_thread(_run)
