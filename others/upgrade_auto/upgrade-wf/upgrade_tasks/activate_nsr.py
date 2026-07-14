"""Task: re-activate NSR / GRES after the upgrade (idempotent).

Restores graceful-switchover, nonstop-routing and nonstop-bridging in one
atomic commit — but only when GRES was ``Enabled`` before the upgrade, as
captured by ``deactivate_nsr``. When it was not enabled, this is a no-op.
"""

import asyncio

from temporalio import activity

from ._common import _connect
from .models import DeviceConnection

_ACTIVATE_COMMANDS = [
    "activate chassis redundancy graceful-switchover",
    "activate routing-options nonstop-routing",
    "activate protocols layer2-control nonstop-bridging",
]


@activity.defn
async def activate_nsr(conn: DeviceConnection, previous_gres_state: str) -> None:
    """Re-activate NSR/GRES only if it was ``Enabled`` before the upgrade."""

    if previous_gres_state != "Enabled":
        return

    def _run() -> None:
        with _connect(conn) as dev:
            dev.edit_config(
                payload="\n".join(_ACTIVATE_COMMANDS),
                fmt="set",
                comment="activate NSR/GRES (upgrade workflow)",
            )

    await asyncio.to_thread(_run)
