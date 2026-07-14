"""Library of device-upgrade tasks (Temporal activities).

Each task lives in its own module and is re-exported here so the workflow can
import them from a single place. ``ALL_ACTIVITIES`` is the list to register on
the Temporal worker.
"""

from .models import (
    ApprovalDecision,
    DeviceConnection,
    DeviceFacts,
    SnapshotResult,
    UpgradeInput,
    UpgradeReport,
)

from .collect_facts import collect_facts
from .take_snapshot import take_snapshot
from .drain_traffic import drain_traffic
from .undrain_traffic import undrain_traffic
from .deactivate_nsr import deactivate_nsr
from .activate_nsr import activate_nsr
from .upload_release import upload_release
from .trigger_upgrade import trigger_upgrade
from .reboot_device import reboot_device
from .wait_until_reachable import wait_until_reachable
from .sanity_check import sanity_check
from .handle_bad_state import handle_bad_state
from .handle_hw_failure import handle_hw_failure
from .compare_snapshots import compare_snapshots
from .generate_report import generate_report

# Register this list on the Temporal worker.
ALL_ACTIVITIES = [
    collect_facts,
    take_snapshot,
    drain_traffic,
    undrain_traffic,
    deactivate_nsr,
    activate_nsr,
    upload_release,
    trigger_upgrade,
    reboot_device,
    wait_until_reachable,
    sanity_check,
    handle_bad_state,
    handle_hw_failure,
    compare_snapshots,
    generate_report,
]

__all__ = [
    # models
    "ApprovalDecision",
    "DeviceConnection",
    "DeviceFacts",
    "SnapshotResult",
    "UpgradeInput",
    "UpgradeReport",
    # activities
    "collect_facts",
    "take_snapshot",
    "drain_traffic",
    "undrain_traffic",
    "deactivate_nsr",
    "activate_nsr",
    "upload_release",
    "trigger_upgrade",
    "reboot_device",
    "wait_until_reachable",
    "sanity_check",
    "handle_bad_state",
    "handle_hw_failure",
    "compare_snapshots",
    "generate_report",
    "ALL_ACTIVITIES",
]
