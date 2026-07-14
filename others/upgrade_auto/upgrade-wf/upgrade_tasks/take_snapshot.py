"""Task: capture an operational-state snapshot and save it to local disk."""

import asyncio
import os
import time
from datetime import datetime
from typing import Optional

from temporalio import activity

from ._common import SNAPSHOT_COMMANDS, JuniperDeviceError, _connect, logger
from .models import DeviceConnection, SnapshotResult

# Base directory under which per-device snapshot folders are written when the
# upgrade input does not specify one.
DEFAULT_SNAPSHOT_DIR = os.path.join(os.getcwd(), "snapshots")


def _save_snapshot(
    conn: DeviceConnection, label: str, data: dict, snapshot_dir: Optional[str]
) -> str:
    """Persist a snapshot to local disk under <snapshot_dir>/<router>_<date>/.

    Returns the folder path the snapshot was written to.
    """
    base_dir = snapshot_dir or DEFAULT_SNAPSHOT_DIR
    date_str = datetime.now().strftime("%Y-%m-%d")
    folder = os.path.join(base_dir, f"{conn.host}_{date_str}")
    os.makedirs(folder, exist_ok=True)
    for cmd, output in data.items():
        # Turn the show command into a safe file name.
        fname = cmd.replace(" ", "_").replace("/", "_") + ".txt"
        with open(os.path.join(folder, f"{label}-{fname}"), "w", encoding="utf-8") as fh:
            fh.write(output)
    return folder


@activity.defn
async def take_snapshot(
    conn: DeviceConnection, label: str, snapshot_dir: Optional[str] = None
) -> SnapshotResult:
    """Capture operational state (a set of show commands) as a snapshot."""

    def _run() -> dict:
        data: dict = {}
        with _connect(conn) as dev:
            for cmd in SNAPSHOT_COMMANDS:
                try:
                    data[cmd] = dev.show(cmd, fmt="text")
                except JuniperDeviceError as err:
                    data[cmd] = f"<command failed: {err}>"
        return data

    data = await asyncio.to_thread(_run)
    saved_path = await asyncio.to_thread(_save_snapshot, conn, label, data, snapshot_dir)
    logger.info("Saved %s snapshot for %s to %s", label, conn.host, saved_path)
    snapshot_id = f"{label}-{conn.host}-{int(time.time())}"
    return SnapshotResult(snapshot_id=snapshot_id, data=data, saved_path=saved_path)
