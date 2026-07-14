"""
Device Upgrade Workflow — Temporal Python SDK
=============================================
Orchestrates a full network device upgrade cycle with:
  - Human-in-the-loop approval gates (Signal-based)
  - Per-step failure handling with compensation actions
  - Device reachability polling (up to 30 min)
  - Pre/post snapshot comparison
  - Final upgrade report generation

Device interaction is done through the `juniper_api` library
(`juniper_api.JuniperDevice`): `show()` + `select()` for facts/snapshots,
`upload()` for the image (with on-device MD5 verification), `upgrade()` to
install, `reboot()` to restart, and `edit_config()` to drain/undrain traffic.
Connection credentials are passed in via `UpgradeInput.connection`.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker

# The upgrade "tasks" live in their own package — one activity per file (see
# ``upgrade_tasks/``). The workflow only orchestrates them; it never talks to
# the device directly. Importing the package pulls in juniper_api (through the
# activity modules), so it is passed through the workflow sandbox unchanged.
with workflow.unsafe.imports_passed_through():
    from upgrade_tasks import (
        ALL_ACTIVITIES,
        DeviceConnection,
        DeviceFacts,
        SnapshotResult,
        UpgradeInput,
        UpgradeReport,
        activate_nsr,
        collect_facts,
        compare_snapshots,
        deactivate_nsr,
        drain_traffic,
        generate_report,
        handle_bad_state,
        handle_hw_failure,
        reboot_device,
        sanity_check,
        take_snapshot,
        trigger_upgrade,
        undrain_traffic,
        upload_release,
        wait_until_reachable,
    )

logger = logging.getLogger("device_upgrade")

TASK_QUEUE = "device-upgrade-queue"

# ---------------------------------------------------------------------------
# Shared retry / timeout policies
# ---------------------------------------------------------------------------

DEFAULT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)

NO_RETRY = RetryPolicy(maximum_attempts=1)

SHORT_TIMEOUT = timedelta(minutes=5)
LONG_TIMEOUT  = timedelta(minutes=35)  # covers the 30-min reachability wait


def _root_cause_message(err: BaseException) -> str:
    """Walk the exception chain and return the most informative message.

    Temporal wraps activity failures in ``ActivityError`` whose ``cause`` is an
    ``ApplicationError`` carrying the original message (e.g. the detailed device
    output appended by juniper_api). Plain Python exceptions chain via
    ``__cause__``. The richest message is *not* necessarily the deepest one:
    juniper_api raises ``UpgradeError`` with the full device output and chains
    the generic underlying ``RpcError`` below it. We therefore collect every
    message in the chain and return the longest (most detailed) non-empty one.
    """
    seen: set[int] = set()
    messages: list[str] = []
    current: Optional[BaseException] = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(getattr(current, "message", None) or current).strip()
        if text:
            messages.append(text)
        current = getattr(current, "cause", None) or current.__cause__
    if not messages:
        return str(err)
    return max(messages, key=len)

# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@workflow.defn
class DeviceUpgradeWorkflow:
    """
    Full device upgrade orchestration with operator continue gates.

    At each wait phase the workflow pauses until the operator sends a single
    empty ``operator_continue`` signal (no payload). There is no approve/reject
    — the signal simply means "proceed".
    """

    def __init__(self):
        # Count of continue signals received; each gate consumes new ones.
        self._continue_count: int = 0
        self._current_gate: Optional[str] = None  # for logging / audit

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    @workflow.signal
    async def operator_continue(self) -> None:
        """Empty 'proceed' signal — advances whatever wait phase is active."""
        self._continue_count += 1

    # ------------------------------------------------------------------
    # Query — expose current status to UIs / CLI
    # ------------------------------------------------------------------

    @workflow.query
    def current_step(self) -> str:
        return self._current_gate or "running"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _await_continue(
        self,
        gate: str,
        timeout_minutes: int,
    ) -> None:
        """Block until the operator sends an ``operator_continue`` signal.

        Captures the current signal count on entry and waits for a *new* one,
        so signals sent before this gate started cannot be banked to skip it.
        """
        self._current_gate = gate
        seen = self._continue_count
        try:
            await workflow.wait_condition(
                lambda: self._continue_count > seen,
                timeout=timedelta(minutes=timeout_minutes),
            )
        except asyncio.TimeoutError:
            raise ApplicationError(
                f"Gate '{gate}' timed out after {timeout_minutes} min "
                "waiting for operator to continue",
                non_retryable=True,
            )
        finally:
            self._current_gate = None

    async def _run_activity(self, fn, *args, timeout=SHORT_TIMEOUT, retry=DEFAULT_RETRY, **kwargs):
        return await workflow.execute_activity(
            fn,
            args=list(args),
            start_to_close_timeout=timeout,
            retry_policy=retry,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Main workflow run
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, params: UpgradeInput) -> UpgradeReport:
        conn = params.connection
        report = UpgradeReport(hostname=conn.host)
        remote_path: Optional[str] = None
        pre_snapshot: Optional[SnapshotResult] = None
        gres_state: str = "Disabled"  # captured by deactivate_nsr, restored later

        try:
            # ── Step 1: Collect facts ──────────────────────────────────
            workflow.logger.info("Step 1 — Collecting facts")
            try:
                facts: DeviceFacts = await self._run_activity(
                    collect_facts, conn, retry=NO_RETRY
                )
            except ActivityError as e:
                report.failure_reason = f"Facts collection failed: {e}"
                report.steps.append("collect_facts: FAILED")
                report.success = False
                return report  # Hard stop — nothing to clean up

            report.steps.append(f"collect_facts: OK — model={facts.model}")

            # ── Step 2: Pre-upgrade snapshot ───────────────────────────
            workflow.logger.info("Step 2 — Pre-upgrade snapshot")
            try:
                pre_snapshot = await self._run_activity(
                    take_snapshot, conn, "pre", params.snapshot_dir
                )
                report.pre_snapshot = pre_snapshot.snapshot_id
                report.steps.append(
                    f"pre_snapshot: {pre_snapshot.snapshot_id} "
                    f"(saved to {pre_snapshot.saved_path})"
                )
            except ActivityError as e:
                report.steps.append(f"pre_snapshot: FAILED — {e}")
                raise  # propagate to outer handler

            # ── Step 3: Drain traffic ──────────────────────────────────
            workflow.logger.info("Step 3 — Draining traffic")
            await self._run_activity(drain_traffic, conn, params.drain_payload)
            report.steps.append("drain_traffic: OK")

            # ── Step 4: Deactivate NSR / GRES ─────────────────────────
            workflow.logger.info("Step 4 — Deactivating NSR/GRES")
            gres_state = await self._run_activity(deactivate_nsr, conn)
            report.steps.append(f"deactivate_nsr: gres_was={gres_state}")

            # ── Step 5: Upload release (transfer + MD5 verify) ─────────
            workflow.logger.info("Step 5 — Uploading release")
            try:
                remote_path = await self._run_activity(
                    upload_release,
                    conn,
                    params.image_path,
                    params.remote_path,
                    params.method,
                    params.target_release_md5,
                    params.copy_to_backup,
                    params.scp_socket_timeout,
                    # Derive the activity timeout from the SCP socket timeout,
                    # adding a 5-minute buffer for MD5 verification + overhead.
                    timeout=timedelta(seconds=params.scp_socket_timeout + 300),
                )
                report.steps.append(f"upload_release: OK — {remote_path}")
            except ActivityError as e:
                report.steps.append(f"upload_release: FAILED — {e}")
                await self._run_activity(activate_nsr, conn, gres_state)
                await self._run_activity(undrain_traffic, conn, params.undrain_payload)
                raise

            # ── Step 6: Operator continue — before upgrade ────────────
            workflow.logger.info("Step 6 — Waiting for operator to continue (upgrade)")
            await self._await_continue("upgrade", params.approval_timeout_minutes)
            report.steps.append("upgrade_gate: operator continued")

            # ── Step 7: Trigger upgrade ────────────────────────────────
            workflow.logger.info("Step 7 — Triggering upgrade")
            await self._run_activity(
                trigger_upgrade, conn, params.image_path, params.remote_path,
                timeout=timedelta(minutes=30), retry=NO_RETRY,
            )
            report.steps.append("trigger_upgrade: OK")

            # ── Step 8: Operator continue — before reboot ─────────────
            workflow.logger.info("Step 8 — Waiting for operator to continue (reboot)")
            await self._await_continue("reboot", params.approval_timeout_minutes)
            report.steps.append("reboot_gate: operator continued")

            # ── Step 9: Reboot ─────────────────────────────────────────
            workflow.logger.info("Step 9 — Rebooting device")
            await self._run_activity(
                reboot_device, conn, params.image_path, retry=NO_RETRY
            )
            report.steps.append("reboot: OK")

            # Give the device a moment to actually go down before we start
            # polling for it to come back (durable timer, survives restarts).
            workflow.logger.info("Waiting 1 min before reachability polling")
            await asyncio.sleep(60)

            # ── Step 10: Wait for device to come back ───────────────────
            workflow.logger.info("Step 10 — Waiting for device (max 30 min)")
            try:
                await self._run_activity(
                    wait_until_reachable, conn,
                    timeout=LONG_TIMEOUT, retry=NO_RETRY,
                )
                report.steps.append("wait_reachable: OK")
            except ActivityError as e:
                report.steps.append(f"wait_reachable: TIMEOUT — {e}")
                report.success = False
                report.failure_reason = str(e)
                return report

            # ── Step 11: Sanity check ──────────────────────────────────
            workflow.logger.info("Step 11 — Sanity check")
            try:
                check = await self._run_activity(sanity_check, conn)
                report.steps.append(f"sanity_check: ok={check['ok']}")
            except ActivityError as e:
                report.steps.append(f"sanity_check: FAILED — {e}")
                raise

            if not check["ok"]:
                if check.get("hw_failure"):
                    workflow.logger.warning("HW failure detected — running compensation")
                    await self._run_activity(
                        handle_hw_failure, conn, check["details"]
                    )
                    report.steps.append("handle_hw_failure: triggered")
                else:
                    workflow.logger.warning("Bad state detected — running compensation")
                    await self._run_activity(
                        handle_bad_state, conn, check["details"]
                    )
                    report.steps.append("handle_bad_state: triggered")

                report.success = False
                report.failure_reason = check.get("details", "sanity check failed")
                # Do NOT undrain — device is not healthy
                return report

            # ── Step 12: Operator continue — before undrain ───────────
            workflow.logger.info("Step 12 — Waiting for operator to continue (undrain)")
            await self._await_continue("undrain", params.approval_timeout_minutes)
            report.steps.append("undrain_gate: operator continued")

            # ── Step 13: Re-activate NSR / GRES ───────────────────────
            workflow.logger.info("Step 13 — Re-activating NSR/GRES")
            await self._run_activity(activate_nsr, conn, gres_state)
            report.steps.append(
                "activate_nsr: restored"
                if gres_state == "Enabled"
                else "activate_nsr: skipped"
            )

            # ── Step 14: Undrain ───────────────────────────────────────
            workflow.logger.info("Step 14 — Undraining traffic")
            await self._run_activity(undrain_traffic, conn, params.undrain_payload)
            report.steps.append("undrain_traffic: OK")

            # ── Step 15: Post-upgrade snapshot + diff ──────────────────
            workflow.logger.info("Step 15 — Post-upgrade snapshot")
            post_snapshot: SnapshotResult = await self._run_activity(
                take_snapshot, conn, "post", params.snapshot_dir
            )
            report.post_snapshot = post_snapshot.snapshot_id
            report.steps.append(f"post_snapshot: {post_snapshot.snapshot_id}")

            diff = await self._run_activity(
                compare_snapshots, pre_snapshot, post_snapshot
            )
            report.snapshot_diff = diff
            report.steps.append("snapshot_diff: computed")

            report.success = True

        except Exception as e:  # noqa: BLE001
            report.success = False
            report.failure_reason = _root_cause_message(e)
            report.steps.append(f"FATAL: {report.failure_reason}")

        finally:
            # ── Step 16: Generate report ───────────────────────────────
            workflow.logger.info("Step 16 — Generating upgrade report")
            try:
                report_path = await self._run_activity(
                    generate_report, report, retry=NO_RETRY
                )
                report.steps.append(f"report: {report_path}")
            except Exception as e:  # noqa: BLE001
                report.steps.append(f"report_generation: FAILED — {e}")

        return report


# ---------------------------------------------------------------------------
# Worker entry point
# ---------------------------------------------------------------------------

async def run_worker():
    client = await Client.connect("localhost:7233")
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[DeviceUpgradeWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        logger.info("Worker started on queue '%s'", TASK_QUEUE)
        await asyncio.Future()  # run forever


# ---------------------------------------------------------------------------
# Starter helper
# ---------------------------------------------------------------------------

async def start_upgrade(
    host: str,
    user: str,
    passwd: str,
    image_path: str,
    md5: str,
):
    client = await Client.connect("localhost:7233")
    handle = await client.start_workflow(
        DeviceUpgradeWorkflow.run,
        UpgradeInput(
            connection=DeviceConnection(host=host, user=user, passwd=passwd),
            image_path=image_path,
            target_release_md5=md5,
        ),
        id=f"upgrade-{host}",
        task_queue=TASK_QUEUE,
        execution_timeout=timedelta(hours=4),
    )
    print(f"Workflow started: {handle.id}")
    return handle


# ---------------------------------------------------------------------------
# Continue helper — send the empty 'proceed' signal from CLI / another script
# ---------------------------------------------------------------------------

async def send_continue(workflow_id: str):
    """Send the single empty 'continue' signal to advance the current gate."""
    client = await Client.connect("localhost:7233")
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(DeviceUpgradeWorkflow.operator_continue)
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
        # python device_upgrade_workflow.py start <host> <user> <passwd> <image_path> <md5>
        asyncio.run(
            start_upgrade(
                sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
            )
        )

    elif cmd == "continue":
        # python device_upgrade_workflow.py continue <workflow-id>
        asyncio.run(send_continue(sys.argv[2]))

    else:
        print("Usage:")
        print("  python device_upgrade_workflow.py worker")
        print("  python device_upgrade_workflow.py start <host> <user> <passwd> <image_path> <md5>")
        print("  python device_upgrade_workflow.py continue <workflow-id>")
