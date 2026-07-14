"""Shared data models for the Ansible-driven upgrade workflow and its activity.

This module must stay free of any heavy imports (no ``ansible``, no
``juniper_api``) so it can be imported safely from inside the Temporal
workflow sandbox.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class UpgradeInput:
    """Everything the workflow needs — designed to be pasted as JSON in the
    Temporal Web UI.

    The inventory (``hosts``) and per-group overrides (``group_vars``) are part
    of this input so an operator can drive a run entirely from the UI without
    editing files on the worker. They are materialised into a temporary
    ``ansible-playbook -i <inventory>`` at runtime.
    """

    # ---- Targeting ----
    target_hosts: str                       # ansible --limit (a host or group)

    # ---- Credentials (mapped onto ansible_user/ansible_password by group_vars) ----
    user: str
    password: Optional[str] = None

    # ---- Image / upgrade parameters ----
    release_file: str = ""                  # image filename present in local_repo
    local_repo: str = "/var/tmp/images"     # controller folder holding image + .md5
    re_target: str = "both"                 # both | re0 | re1 (Junos)

    # ---- Inventory materialised from JSON ----
    # mapping of inventory hostname -> host vars (device_name, model, software, ...)
    hosts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # junos_devices group-var overrides (overload_* flags, whitelists, ...)
    group_vars: Dict[str, Any] = field(default_factory=dict)
    # extra vars applied to every playbook run (highest precedence)
    extra_vars: Dict[str, Any] = field(default_factory=dict)

    # ---- Behaviour ----
    approval_timeout_minutes: int = 30      # how long a gate waits for the operator
    # Absolute path to the Ansible root (containing playbooks/) on the worker.
    # When null, the bundled ``ansible/`` directory of this tool is used.
    playbooks_root: Optional[str] = None


@dataclass
class PlaybookRequest:
    """A single ``ansible-playbook`` invocation."""

    playbook: str                           # filename under playbooks/ (e.g. check_node.yml)
    extra_vars: Dict[str, Any] = field(default_factory=dict)  # per-call overrides
    timeout_minutes: int = 15               # informational; the workflow sets the real timeout


@dataclass
class PlaybookResult:
    playbook: str
    rc: int
    ok: bool
    stdout_tail: str = ""


@dataclass
class UpgradeReport:
    target: str
    success: bool = False
    steps: List[str] = field(default_factory=list)
    failure_reason: Optional[str] = None
