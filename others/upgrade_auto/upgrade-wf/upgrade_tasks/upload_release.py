"""Task: upload the Junos image and verify its MD5 on the device."""

import asyncio
import os

from temporalio import activity

from ._common import _connect
from .models import DeviceConnection


@activity.defn
async def upload_release(
    conn: DeviceConnection,
    local_file: str,
    remote_path: str,
    method: str,
    expected_md5: str,
    copy_to_backup: bool,
    scp_socket_timeout: float = 600.0,
) -> str:
    """Upload the image, verify its MD5 on the device, and return the remote path.

    juniper_api's ``upload`` performs the transfer, the on-device MD5 check
    (when ``md5`` is given) and the optional master -> backup RE copy in one call.
    """

    def _run() -> str:
        with _connect(conn) as dev:
            dev.upload(
                local_file,
                remote_path=remote_path,
                method=method,
                md5=expected_md5,
                copy_to_backup=copy_to_backup,
                scp_socket_timeout=scp_socket_timeout,
            )
        return remote_path.rstrip("/") + "/" + os.path.basename(local_file)

    return await asyncio.to_thread(_run)
