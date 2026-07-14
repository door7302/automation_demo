"""Shared data models for the device-upgrade workflow and its tasks.

This module must stay free of any heavy/device imports (e.g. ``juniper_api``)
so it can be imported safely from inside the Temporal workflow sandbox.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


@dataclass
class DeviceFacts:
    hostname: str
    model: str
    sw_version: str
    reachable: bool


@dataclass
class SnapshotResult:
    snapshot_id: str
    data: dict
    saved_path: Optional[str] = None


@dataclass
class DeviceConnection:
    """Connection / credential parameters passed to every device activity."""
    host: str
    user: str
    passwd: Optional[str] = None
    port: int = 830
    ssh_private_key_file: Optional[str] = None


@dataclass
class UpgradeInput:
    connection: DeviceConnection
    image_path: str               # local path to the Junos install image
    target_release_md5: str       # expected MD5 of that image
    remote_path: str = "/var/tmp" # destination directory on the device
    method: str = "scp"           # "scp" or "ftp"
    copy_to_backup: bool = True    # copy image master RE -> backup RE
    # A list of set/delete/activate/deactivate commands, applied in order as a
    # single atomic commit.
    drain_payload: List[str] = field(
        default_factory=lambda: ["set protocols isis overload"]
    )
    undrain_payload: List[str] = field(
        default_factory=lambda: ["delete protocols isis overload"]
    )
    approval_timeout_minutes: int = 30
    snapshot_dir: Optional[str] = None  # local dir to save snapshots into
    scp_socket_timeout: float = 600.0   # per-channel SCP timeout for the upload


@dataclass
class UpgradeReport:
    hostname: str
    success: bool = False
    steps: list = field(default_factory=list)
    pre_snapshot: Optional[str] = None
    post_snapshot: Optional[str] = None
    snapshot_diff: Optional[dict] = None
    failure_reason: Optional[str] = None


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
