"""Helpers to build an inventory + extra-vars and run ``ansible-playbook``.

The playbooks are bundled with this tool under ``ansible/`` and are executed in
that directory (so their ``{{ playbook_dir }}`` / ``state_dir`` logic and
``snapshot_commands.txt`` keep working). This module only generates a
throw-away inventory from the JSON input and passes the operator-supplied
values as extra-vars (highest Ansible precedence, so the JSON always wins).
"""

import json
import logging
import os
from typing import Any, Callable, Dict, Tuple

from .models import PlaybookRequest, UpgradeInput

logger = logging.getLogger("ansible_upgrade")

# Group in inventory.yml whose hosts the playbooks target by default.
DEVICE_GROUP = "junos_devices"

# Resolve the bundled Ansible content (playbooks + inventory + group_vars +
# ansible.cfg) that ships alongside this tool. Layout:
#   upgrade_tasks -> upgrade-wf -> upgrade_temporal_ansible -> ansible/
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOL_DIR = os.path.abspath(os.path.join(_PKG_DIR, "..", ".."))
DEFAULT_PLAYBOOKS_ROOT = os.path.join(_TOOL_DIR, "ansible")


def resolve_playbooks_root(inp: UpgradeInput) -> str:
    """Absolute path to the Ansible root (containing playbooks/) on the worker."""
    root = (
        inp.playbooks_root
        or os.environ.get("UPGRADE_ANSIBLE_ROOT")
        or DEFAULT_PLAYBOOKS_ROOT
    )
    return os.path.abspath(os.path.expanduser(root))


def build_inventory_dict(inp: UpgradeInput) -> Dict[str, Any]:
    """Materialise a YAML inventory (all.children.junos_devices.hosts.*)."""
    hosts: Dict[str, Any] = {}
    for name, host_vars in (inp.hosts or {}).items():
        hosts[name] = dict(host_vars or {})
    return {"all": {"children": {DEVICE_GROUP: {"hosts": hosts}}}}


def build_extra_vars(inp: UpgradeInput, req: PlaybookRequest) -> Dict[str, Any]:
    """Merge the operator input into a single extra-vars dict.

    Precedence (low -> high): base input < group_vars overrides <
    input.extra_vars < per-call req.extra_vars.
    """
    ev: Dict[str, Any] = {
        "target_hosts": inp.target_hosts,
        "user": inp.user,
    }
    if inp.password is not None:
        ev["password"] = inp.password
    if inp.release_file:
        ev["release_file"] = inp.release_file
    if inp.local_repo:
        ev["local_repo"] = inp.local_repo
    if inp.re_target:
        ev["re_target"] = inp.re_target
    ev.update(inp.group_vars or {})
    ev.update(inp.extra_vars or {})
    ev.update(req.extra_vars or {})
    return ev


def build_command(
    inp: UpgradeInput, req: PlaybookRequest, inventory_path: str
) -> list:
    """Assemble the ``ansible-playbook`` argv."""
    extra_vars = build_extra_vars(inp, req)
    return [
        "ansible-playbook",
        os.path.join("playbooks", req.playbook),
        "-i",
        inventory_path,
        "--limit",
        inp.target_hosts,
        "-e",
        json.dumps(extra_vars),
    ]


def write_inventory(inp: UpgradeInput, directory: str) -> str:
    """Write the generated inventory into ``directory`` and return its path.

    The file is written *inside the Ansible root* (not a scratch temp dir) so
    that the adjacent ``group_vars/junos_devices.yml`` — which wires
    ``device_name``/``user``/``password`` onto the PyEZ connection — is
    auto-discovered by Ansible (group_vars are resolved relative to the
    inventory source). A unique name avoids clashes between concurrent runs.
    """
    import uuid

    import yaml  # provided by ansible (PyYAML is an ansible dependency)

    inv_path = os.path.join(directory, f".inventory.generated.{uuid.uuid4().hex}.yml")
    with open(inv_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(build_inventory_dict(inp), fh, default_flow_style=False)
    return inv_path


async def run_ansible_playbook(
    inp: UpgradeInput,
    req: PlaybookRequest,
    heartbeat: Callable[[str], None],
    tail_lines: int = 60,
) -> Tuple[int, str]:
    """Run one playbook as a subprocess, streaming output through ``heartbeat``.

    Returns ``(returncode, stdout_tail)``. Runs with cwd = the Ansible tool root
    so ``ansible.cfg`` (collections, callbacks) and relative paths resolve.
    """
    import asyncio
    from collections import deque

    root = resolve_playbooks_root(inp)
    playbook_file = os.path.join(root, "playbooks", req.playbook)
    if not os.path.isfile(playbook_file):
        raise FileNotFoundError(
            f"Playbook not found: {playbook_file} "
            f"(set playbooks_root or UPGRADE_ANSIBLE_ROOT)"
        )

    # Write the generated inventory into the Ansible root so the adjacent
    # group_vars/ directory is picked up. Clean it up when we are done.
    inv_path = write_inventory(inp, root)
    try:
        cmd = build_command(inp, req, inv_path)
        logger.info("Running: %s (cwd=%s)", " ".join(cmd[:3]) + " ...", root)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        tail: "deque[str]" = deque(maxlen=tail_lines)
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            tail.append(line)
            heartbeat(line)

        rc = await proc.wait()
        return rc, "\n".join(tail)
    finally:
        try:
            os.remove(inv_path)
        except OSError:
            pass
