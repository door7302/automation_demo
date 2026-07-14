"""Task: deactivate NSR / GRES before the upgrade (idempotent).

Reads the current stateful-replication (GRES) state via ``show task
replication`` and, when it is ``Enabled``, deactivates graceful-switchover,
nonstop-routing and nonstop-bridging in one atomic commit.

The captured ``task-gres-state`` value is returned so the workflow can pass it
to ``activate_nsr`` and only restore what was enabled before.
"""

import asyncio

from temporalio import activity

from ._common import _connect
from .models import DeviceConnection

_DEACTIVATE_COMMANDS = [
    "deactivate chassis redundancy graceful-switchover",
    "deactivate routing-options nonstop-routing",
    "deactivate protocols layer2-control nonstop-bridging",
]


@activity.defn
async def deactivate_nsr(conn: DeviceConnection) -> str:
    """Deactivate NSR/GRES when enabled. Returns the captured GRES state
    (e.g. ``"Enabled"`` or ``"Disabled"``) for later restoration."""

    def _run() -> str:
        with _connect(conn) as dev:
            repl_xml = dev.show("show task replication", fmt="xml")
            gres_state = (
                dev.select(repl_xml, "task-gres-state", first=True) or "Disabled"
            )

            if gres_state == "Enabled":
                dev.edit_config(
                    payload="\n".join(_DEACTIVATE_COMMANDS),
                    fmt="set",
                    comment="deactivate NSR/GRES (upgrade workflow)",
                )
        return gres_state

    return await asyncio.to_thread(_run)
