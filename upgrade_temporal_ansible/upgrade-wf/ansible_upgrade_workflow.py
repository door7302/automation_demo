"""
Ansible-driven Device Upgrade Workflow — Temporal Python SDK
============================================================
Re-implements the AWX "Junos Guided Upgrade" workflow (see
``tools/upgrade_ansible/provision_awx.yml``) as a Temporal workflow, but
instead of AWX job templates each node runs one of the existing Ansible
playbooks under ``tools/upgrade_ansible/playbooks/`` via ``ansible-playbook``.

Key ideas
---------
* One generic activity (``run_playbook``) shells out to ``ansible-playbook``.
* Inventory + group-var overrides + credentials are supplied as JSON input
  (``UpgradeInput``) — an operator can drive a whole run from the Temporal
  Web UI without touching files on the worker.
* AWX ``workflow_approval`` nodes become human-in-the-loop **continue gates**
  (the operator sends an empty ``operator_continue`` signal).
* AWX ``failure_nodes`` unwind chains become an in-workflow **compensation
  stack**: each drain step registers its inverse, and any failure during
  drain / upload / upgrade runs the registered inverses in reverse order,
  then captures an ``after`` snapshot + diff.

Flow (mirrors the AWX graph)
----------------------------
pre-checks (check_node, check_routing_engines, snapshot before)
  → GATE drain
  → drain (set_isis_overload, deactivate_bgp_groups, deactivate_ri,
           shut_interfaces, deactivate_gres_nsr)
  → GATE upload → upload_release
  → GATE upgrade → upgrade_software
  → GATE reboot → reboot → wait (check_node)
  → post-checks (check_node, check_routing_engines, check_fpc_online)
  → restore (unshut_interfaces, activate_ri, activate_bgp_groups,
             restore_isis_overload, activate_gres_nsr)
  → check_replication_state → snapshot after → diff_snapshots
"""

import asyncio
import logging
from datetime import timedelta
from typing import List, Optional, Tuple

from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from upgrade_tasks import (
        ALL_ACTIVITIES,
        PlaybookRequest,
        UpgradeInput,
        UpgradeReport,
        run_playbook,
    )

logger = logging.getLogger("ansible_upgrade")

TASK_QUEUE = "ansible-upgrade-queue"

# ---------------------------------------------------------------------------
# Retry / timeout policies
# ---------------------------------------------------------------------------

DEFAULT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
NO_RETRY = RetryPolicy(maximum_attempts=1)

SHORT_TIMEOUT = timedelta(minutes=10)
# Long enough for upload (30-min SCP), install, reboot + the 30-min
# reachability wait baked into check_node.yml.
LONG_TIMEOUT = timedelta(minutes=40)

HEARTBEAT_TIMEOUT = timedelta(minutes=2)

# Inverse playbook used to undo each drain step (for compensation / restore).
INVERSE = {
    "set_isis_overload.yml": "restore_isis_overload.yml",
    "deactivate_bgp_groups.yml": "activate_bgp_groups.yml",
    "deactivate_ri.yml": "activate_ri.yml",
    "shut_interfaces.yml": "unshut_interfaces.yml",
    "deactivate_gres_nsr.yml": "activate_gres_nsr.yml",
}


@workflow.defn
class AnsibleUpgradeWorkflow:
    """Guided upgrade orchestration with operator continue gates.

    At each gate the workflow pauses until the operator sends a single empty
    ``operator_continue`` signal (no payload) — the AWX "approve" equivalent.
    """

    def __init__(self) -> None:
        self._continue_count: int = 0
        self._current_gate: Optional[str] = None

    # ------------------------------------------------------------------
    # Signals / queries
    # ------------------------------------------------------------------

    @workflow.signal
    async def operator_continue(self) -> None:
        """Empty 'proceed' signal — advances whatever gate is active."""
        self._continue_count += 1

    @workflow.query
    def current_step(self) -> str:
        return self._current_gate or "running"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _await_continue(self, gate: str, timeout_minutes: int) -> None:
        self._current_gate = gate
        seen = self._continue_count
        try:
            await workflow.wait_condition(
                lambda: self._continue_count > seen,
                timeout=timedelta(minutes=timeout_minutes),
                # Label the backing timer so the human gate is identifiable
                # in the Temporal Web UI timeline.
                timeout_summary=f"gate: {gate}",
            )
        except asyncio.TimeoutError:
            raise ApplicationError(
                f"Gate '{gate}' timed out after {timeout_minutes} min "
                "waiting for operator to continue",
                non_retryable=True,
            )
        finally:
            self._current_gate = None

    async def _pb(
        self,
        params: UpgradeInput,
        playbook: str,
        extra_vars: Optional[dict] = None,
        *,
        timeout: timedelta = SHORT_TIMEOUT,
        retry: RetryPolicy = NO_RETRY,
    ):
        return await workflow.execute_activity(
            run_playbook,
            args=[params, PlaybookRequest(playbook=playbook, extra_vars=extra_vars or {})],
            start_to_close_timeout=timeout,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=retry,
            # Distinguish otherwise-identical "run_playbook" activities in the
            # Temporal Web UI timeline by labelling each one with its playbook.
            summary=playbook,
        )

    async def _rollback(
        self, params: UpgradeInput, compensations: List[str], report: UpgradeReport
    ) -> None:
        """Undo applied drain steps (reverse order), then snapshot + diff."""
        workflow.logger.warning("Rolling back drain steps")
        for playbook in reversed(compensations):
            try:
                await self._pb(params, playbook, timeout=SHORT_TIMEOUT, retry=DEFAULT_RETRY)
                report.steps.append(f"rollback {playbook}: OK")
            except ActivityError as e:  # keep unwinding even if one step fails
                report.steps.append(f"rollback {playbook}: FAILED — {e}")
        for playbook, ev in (
            ("snapshot.yml", {"snapshot_label": "after"}),
            # Diff is informational during rollback (volatile counters will
            # differ) and deterministic, so don't retry it.
            ("diff_snapshots.yml", {"diff_fail_on_change": False}),
        ):
            try:
                await self._pb(params, playbook, ev, retry=DEFAULT_RETRY)
                report.steps.append(f"rollback {playbook}: OK")
            except ActivityError as e:
                report.steps.append(f"rollback {playbook}: FAILED — {e}")

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, params: UpgradeInput) -> UpgradeReport:
        report = UpgradeReport(target=params.target_hosts)
        # Playbooks to run (in reverse) to undo the drain if something fails.
        compensations: List[str] = []
        gate_timeout = params.approval_timeout_minutes

        try:
            # ── Pre-checks ─────────────────────────────────────────────
            for playbook in ("check_node.yml", "check_routing_engines.yml"):
                workflow.logger.info("Pre-check: %s", playbook)
                await self._pb(params, playbook, timeout=LONG_TIMEOUT, retry=DEFAULT_RETRY)
                report.steps.append(f"{playbook}: OK")

            await self._pb(params, "snapshot.yml", {"snapshot_label": "before"})
            report.steps.append("snapshot before: OK")

            # ── Gate: drain ────────────────────────────────────────────
            await self._await_continue("drain", gate_timeout)

            # ── Drain (each step registers its inverse for rollback) ───
            drain_steps: List[Tuple[str, Optional[dict]]] = [
                ("set_isis_overload.yml", None),
                ("deactivate_bgp_groups.yml", None),
                ("deactivate_ri.yml", None),
                ("shut_interfaces.yml", None),
                ("deactivate_gres_nsr.yml", None),
            ]
            try:
                for playbook, ev in drain_steps:
                    workflow.logger.info("Drain: %s", playbook)
                    await self._pb(params, playbook, ev)
                    if playbook in INVERSE:
                        compensations.append(INVERSE[playbook])
                    report.steps.append(f"{playbook}: OK")
            except ActivityError as e:
                report.steps.append(f"drain FAILED — {e}")
                await self._rollback(params, compensations, report)
                report.success = False
                report.failure_reason = f"drain failed: {e}"
                return report

            # ── Gate: upload ───────────────────────────────────────────
            await self._await_continue("upload", gate_timeout)
            try:
                await self._pb(params, "upload_release.yml", timeout=LONG_TIMEOUT)
                report.steps.append("upload_release: OK")
            except ActivityError as e:
                report.steps.append(f"upload_release: FAILED — {e}")
                await self._rollback(params, compensations, report)
                report.success = False
                report.failure_reason = f"upload failed: {e}"
                return report

            # ── Gate: upgrade ──────────────────────────────────────────
            await self._await_continue("upgrade", gate_timeout)
            try:
                await self._pb(
                    params,
                    "upgrade_software.yml",
                    {"re_target": params.re_target},
                    timeout=LONG_TIMEOUT,
                )
                report.steps.append("upgrade_software: OK")
            except ActivityError as e:
                report.steps.append(f"upgrade_software: FAILED — {e}")
                await self._rollback(params, compensations, report)
                report.success = False
                report.failure_reason = f"upgrade failed: {e}"
                return report

            # ── Gate: reboot ───────────────────────────────────────────
            await self._await_continue("reboot", gate_timeout)
            await self._pb(params, "reboot.yml")
            report.steps.append("reboot: OK")

            # Give the device a moment to actually go down (durable timer).
            # Use workflow.sleep (not asyncio.sleep) so the timer carries its
            # own summary and shows up as a distinct, clearly-named step in the
            # Temporal Web UI timeline instead of being confused with the
            # preceding "gate: reboot" timer.
            workflow.logger.info("Waiting 1 min before reachability polling")
            await workflow.sleep(60, summary="settle: wait before reachability poll")

            # check_node.yml polls up to 30 min for NETCONF to return.
            await self._pb(params, "check_node.yml", timeout=LONG_TIMEOUT)
            report.steps.append("wait reachable (check_node): OK")

            # ── Post-checks ────────────────────────────────────────────
            for playbook in ("check_routing_engines.yml", "check_fpc_online.yml"):
                await self._pb(params, playbook, retry=DEFAULT_RETRY)
                report.steps.append(f"{playbook}: OK")

            # ── Restore service (reverse of drain) ─────────────────────
            for playbook in reversed(compensations):
                workflow.logger.info("Restore: %s", playbook)
                await self._pb(params, playbook)
                report.steps.append(f"{playbook}: OK")
            compensations.clear()  # service restored; nothing left to undo

            await self._pb(params, "check_replication_state.yml", retry=DEFAULT_RETRY)
            report.steps.append("check_replication_state: OK")

            # ── Post snapshot + diff ───────────────────────────────────
            await self._pb(params, "snapshot.yml", {"snapshot_label": "after"})
            report.steps.append("snapshot after: OK")
            # Report drift but don't fail the (already-succeeded) upgrade on
            # volatile counters; meaningful diffs are still logged.
            await self._pb(params, "diff_snapshots.yml", {"diff_fail_on_change": False})
            report.steps.append("diff_snapshots: OK")

            report.success = True

        except ApplicationError as e:  # gate timeout etc.
            report.success = False
            report.failure_reason = str(e)
            report.steps.append(f"FATAL: {report.failure_reason}")
        except Exception as e:  # noqa: BLE001
            report.success = False
            report.failure_reason = str(e)
            report.steps.append(f"FATAL: {report.failure_reason}")

        return report


# ---------------------------------------------------------------------------
# Worker entry point
# ---------------------------------------------------------------------------

async def run_worker() -> None:
    client = await Client.connect("localhost:7233")
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[AnsibleUpgradeWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        logger.info("Worker started on queue '%s'", TASK_QUEUE)
        await asyncio.Future()  # run forever


# ---------------------------------------------------------------------------
# Starter / continue helpers
# ---------------------------------------------------------------------------

async def start_upgrade(params_json_path: str) -> None:
    """Start a workflow from a JSON file matching ``UpgradeInput``."""
    import json

    with open(params_json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    params = UpgradeInput(**data)

    client = await Client.connect("localhost:7233")
    handle = await client.start_workflow(
        AnsibleUpgradeWorkflow.run,
        params,
        id=f"ansible-upgrade-{params.target_hosts}",
        task_queue=TASK_QUEUE,
        execution_timeout=timedelta(hours=6),
    )
    print(f"Workflow started: {handle.id}")


async def send_continue(workflow_id: str) -> None:
    client = await Client.connect("localhost:7233")
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(AnsibleUpgradeWorkflow.operator_continue)
    print(f"Continue signal sent → {workflow_id}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "worker"

    if cmd == "worker":
        asyncio.run(run_worker())
    elif cmd == "start":
        # python ansible_upgrade_workflow.py start <params.json>
        asyncio.run(start_upgrade(sys.argv[2]))
    elif cmd == "continue":
        # python ansible_upgrade_workflow.py continue <workflow-id>
        asyncio.run(send_continue(sys.argv[2]))
    else:
        print("Usage:")
        print("  python ansible_upgrade_workflow.py worker")
        print("  python ansible_upgrade_workflow.py start <params.json>")
        print("  python ansible_upgrade_workflow.py continue <workflow-id>")
