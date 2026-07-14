"""Task: diff the pre- and post-upgrade snapshots."""

import difflib

from temporalio import activity

from .models import SnapshotResult


@activity.defn
async def compare_snapshots(pre: SnapshotResult, post: SnapshotResult) -> dict:
    """Diff each captured command's output between the pre and post snapshots."""
    diff: dict = {}
    for cmd in pre.data:
        before = pre.data.get(cmd, "").splitlines()
        after = post.data.get(cmd, "").splitlines()
        delta = list(
            difflib.unified_diff(before, after, fromfile="pre", tofile="post", lineterm="")
        )
        if delta:
            diff[cmd] = "\n".join(delta)
    return diff
