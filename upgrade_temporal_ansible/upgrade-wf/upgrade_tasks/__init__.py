"""Ansible-driven upgrade tasks (Temporal activities).

A single generic ``run_playbook`` activity executes the playbooks that live in
the sibling ``upgrade_temporal_ansible`` tool. Models are kept import-light so the
workflow can import them inside the Temporal sandbox.
"""

from .activities import ALL_ACTIVITIES, run_playbook
from .models import (
    PlaybookRequest,
    PlaybookResult,
    UpgradeInput,
    UpgradeReport,
)

__all__ = [
    # models
    "PlaybookRequest",
    "PlaybookResult",
    "UpgradeInput",
    "UpgradeReport",
    # activities
    "run_playbook",
    "ALL_ACTIVITIES",
]
