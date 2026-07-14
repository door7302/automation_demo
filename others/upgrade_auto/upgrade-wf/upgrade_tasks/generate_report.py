"""Task: render the final upgrade report to a text file."""

import os
import tempfile
import time

from temporalio import activity

from ._common import logger
from .models import UpgradeReport


@activity.defn
async def generate_report(report: UpgradeReport) -> str:
    """Render the upgrade report to a text file and return its path."""
    lines = [
        f"Upgrade report — {report.hostname}",
        "=" * 50,
        f"Success        : {report.success}",
        f"Pre-snapshot   : {report.pre_snapshot}",
        f"Post-snapshot  : {report.post_snapshot}",
        f"Failure reason : {report.failure_reason or '-'}",
        "",
        "Steps:",
    ]
    lines += [f"  - {step}" for step in report.steps]
    if report.snapshot_diff:
        lines += ["", "Snapshot diff:"]
        for cmd, delta in report.snapshot_diff.items():
            lines += [f"  [{cmd}]", delta]
    content = "\n".join(lines)

    path = os.path.join(
        tempfile.gettempdir(),
        f"upgrade-report-{report.hostname}-{int(time.time())}.txt",
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    logger.info("Wrote upgrade report to %s", path)
    return path
