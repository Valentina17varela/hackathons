"""Output invariants checked on every answer, independent of any reference.

* every cited evidence id exists in the participant-visible data;
* nothing cited was captured or recorded after the case's ``audit_at``;
* the plan satisfies the step contract (``planner.validate_plan``), which
  includes: a ``COMPLETE`` answer always carries evidence-backed steps.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from models import DeletionStatus, InvalidStepError, Step
from parser import Tables
from planner import validate_plan


class CitationIndex:
    """Maps ``file_id`` / ``activity_id`` / ``audit_id`` to the time it became visible."""

    def __init__(self, tables: Tables) -> None:
        self._times: dict[str, datetime] = {}
        for evidence in tables.evidence_files:
            self._times[evidence.file_id] = evidence.captured_at
        for activity in tables.activities:
            self._times[activity.activity_id] = activity.event_time
        for event in tables.audit_events:
            self._times[event.audit_id] = event.event_time

    def violations(self, steps: Sequence[Step], audit_at: datetime) -> list[str]:
        """Describe every cited id that is unknown or later than ``audit_at``."""
        problems: list[str] = []
        for step in steps:
            if not step.evidence_id:
                continue
            moment = self._times.get(step.evidence_id)
            if moment is None:
                problems.append(f"step {step.step_no}: unknown id {step.evidence_id}")
            elif moment > audit_at:
                problems.append(
                    f"step {step.step_no}: {step.evidence_id} is later than audit_at"
                )
        return problems


def plan_problems(status: DeletionStatus, steps: Sequence[Step]) -> list[str]:
    """Contract violations of a plan (empty list when valid)."""
    try:
        validate_plan(status, steps)
    except InvalidStepError as exc:
        return [str(exc)]
    return []
