"""Temporal activity that runs a single Ansible playbook.

There is one generic activity — ``run_playbook`` — because every step of the
guided upgrade is just an ``ansible-playbook`` invocation against the bundled
``ansible/playbooks`` set. The workflow decides *which* playbook and
*what* per-call vars; the activity does the actual subprocess work and
heartbeats each output line so long-running steps (upload, upgrade, reboot,
reachability wait) are visible and not killed by the heartbeat timeout.
"""

import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError

from ._ansible import run_ansible_playbook
from .models import PlaybookRequest, PlaybookResult, UpgradeInput

logger = logging.getLogger("ansible_upgrade")


@activity.defn
async def run_playbook(inp: UpgradeInput, req: PlaybookRequest) -> PlaybookResult:
    """Run ``playbooks/<req.playbook>`` and fail the activity on non-zero rc."""

    def _heartbeat(line: str) -> None:
        # Surface progress to the Temporal UI and keep the activity alive.
        activity.heartbeat(line)

    rc, tail = await run_ansible_playbook(inp, req, _heartbeat)

    if rc != 0:
        raise ApplicationError(
            f"Playbook '{req.playbook}' failed (rc={rc})\n{tail}",
            type="AnsiblePlaybookError",
        )

    return PlaybookResult(playbook=req.playbook, rc=rc, ok=True, stdout_tail=tail)


ALL_ACTIVITIES = [run_playbook]

__all__ = ["run_playbook", "ALL_ACTIVITIES"]
