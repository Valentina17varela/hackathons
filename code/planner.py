"""Step planner (TASK-09).

Turns an ``Assessment`` into the ordered ``steps_json`` of one case.

Plan shape
----------
* ``NOT_APPLICABLE``: no steps.
* ``INCOMPLETE``: one ``FINDING`` per violation that has a finding operation
  (cited with participant-visible evidence), one ``REMEDIATION`` per violation
  (empty ``evidence_id``) and one ``VERIFICATION`` per judged resource, whether
  it was a violation or already compliant (empty ``evidence_id``: the check is
  future work).
* ``COMPLETE``: only ``VERIFICATION`` steps, each citing the evidence that
  proves the current compliant state.

Ordering
--------
Steps are sorted by the canonical key of the output contract: phase, operation
(table order), ``system_id``, ``resource_id`` (``models.number_steps``). The
operation table already puts ``DISABLE_DATA_FLOW`` and ``BLOCK_RESTORE`` before
every record change, so recreation paths are closed before dependent records
are touched. Records of different systems that share an operation follow the
contract's ``system_id`` order; the public samples confirm that the contract
order, not source-before/after-copy order, is expected.

Evidence
--------
Only evidence at or before ``audit_at`` may be cited (``Citations``). A step
that needs evidence and has none is skipped; ids are never invented. When a
resource is supported by several ids the choice is deterministic:

1. the evidence file that supports most of the cited resources of the case
   (at least two), so one capture of the case is cited consistently;
2. else the investigator's primary proof when it is an evidence file;
3. else the latest-captured evidence file, then the latest activity or audit
   event.

Fixed steps
-----------
Every deletion case is expected to verify the subject's account
(``VERIFY_RECORD_ABSENT``) and security audit record (``VERIFY_RECORD_PRESENT``).
When the assessment holds no judged resource of one of those systems, the step
is still emitted from any resource of that system the investigator found but
could not judge (unknown state); it is skipped when no id was found.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from investigator import ACCOUNT, SECURITY
from models import (
    Assessment,
    DeletionStatus,
    Operation,
    Resource,
    Step,
    number_steps,
    validate_steps,
)
from parser import Tables

logger = logging.getLogger(__name__)

# Verification steps added for every deletion case when the account/security
# system was not judged by the assessment: the account record must be absent
# and the security audit log must still be present.
FIXED_VERIFICATIONS: tuple[tuple[str, Operation], ...] = (
    (ACCOUNT, Operation.VERIFY_RECORD_ABSENT),
    (SECURITY, Operation.VERIFY_RECORD_PRESENT),
)

# A file must support at least this many cited resources to become the
# case-wide citation.
_MIN_SHARED_SUPPORT = 2


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Citations:
    """Ids that may be cited for one case, with their kind and timestamp.

    Built with ``visible_citations``; contains only evidence files, activities
    and audit events dated at or before ``audit_at``.
    """

    times: dict[str, datetime] = field(default_factory=dict)
    files: frozenset[str] = frozenset()

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self.times

    def is_file(self, evidence_id: str) -> bool:
        return evidence_id in self.files

    def rank(self, evidence_id: str) -> tuple[int, float, str]:
        """Sort key: evidence files first, latest first, id as final tie-break."""
        moment = self.times[evidence_id].timestamp()
        return (0 if evidence_id in self.files else 1, -moment, evidence_id)


def visible_citations(tables: Tables, audit_at: datetime) -> Citations:
    """Everything citable at ``audit_at``; later items are excluded."""
    times: dict[str, datetime] = {}
    files: set[str] = set()
    for evidence_file in tables.evidence_files:
        if evidence_file.captured_at <= audit_at:
            times[evidence_file.file_id] = evidence_file.captured_at
            files.add(evidence_file.file_id)
    for activity in tables.activities:
        if activity.event_time <= audit_at:
            times[activity.activity_id] = activity.event_time
    for event in tables.audit_events:
        if event.event_time <= audit_at:
            times[event.audit_id] = event.event_time
    return Citations(times=times, files=frozenset(files))


# ---------------------------------------------------------------------------
# Proof selection
# ---------------------------------------------------------------------------


def _candidates(resource: Resource, citations: Citations | None) -> list[str]:
    """Distinct citable ids supporting ``resource``; the primary proof comes first."""
    seen: dict[str, None] = {}
    for evidence_id in (resource.evidence_id, *getattr(resource, "evidence_ids", ())):
        if evidence_id and (citations is None or evidence_id in citations):
            seen.setdefault(evidence_id, None)
    return list(seen)


class _ProofSelector:
    """Chooses the evidence id cited for each resource of one case."""

    def __init__(self, cited: Iterable[Resource], citations: Citations | None) -> None:
        self._citations = citations
        self._shared: str | None = None
        if citations is None:
            return
        support: Counter[str] = Counter(
            evidence_id
            for resource in cited
            for evidence_id in _candidates(resource, citations)
            if citations.is_file(evidence_id)
        )
        shared = [e for e, n in support.items() if n >= _MIN_SHARED_SUPPORT]
        if shared:
            self._shared = min(shared, key=lambda e: (-support[e], citations.rank(e)))

    def proof(self, resource: Resource) -> str:
        candidates = _candidates(resource, self._citations)
        if not candidates:
            return ""
        citations = self._citations
        if citations is None:
            return candidates[0]
        if self._shared is not None and self._shared in candidates:
            return self._shared
        if citations.is_file(candidates[0]):
            return candidates[0]
        return min(candidates, key=citations.rank)


# ---------------------------------------------------------------------------
# Step construction
# ---------------------------------------------------------------------------


class _Plan:
    """Collects steps without duplicates; numbering happens at the end."""

    def __init__(self) -> None:
        self._steps: dict[tuple[Operation, str, str], Step] = {}

    def add(self, operation: Operation, resource: Resource, evidence_id: str) -> None:
        key = (operation, resource.system_id, resource.resource_id)
        if key not in self._steps:
            self._steps[key] = Step.create(
                1, resource.system_id, resource.resource_id, operation, evidence_id
            )

    def steps(self) -> list[Step]:
        return number_steps(self._steps.values())


def _plan_incomplete(
    plan: _Plan, assessment: Assessment, citations: Citations | None
) -> None:
    findings = [t for t in assessment.violations if t.plan.finding is not None]
    selector = _ProofSelector((t.resource for t in findings), citations)
    for treatment in assessment.violations:
        resource = treatment.resource
        if treatment.plan.finding is not None:
            proof = selector.proof(resource)
            if proof:
                plan.add(treatment.plan.finding, resource, proof)
            else:
                logger.warning(
                    "%s: no citable evidence for %s/%s; finding skipped",
                    assessment.case_id,
                    resource.system_id,
                    resource.resource_id,
                )
        if treatment.plan.remediation is not None:
            plan.add(treatment.plan.remediation, resource, "")
    for treatment in (*assessment.violations, *assessment.compliant):
        plan.add(treatment.plan.verification, treatment.resource, "")


def _plan_complete(
    plan: _Plan, assessment: Assessment, citations: Citations | None
) -> None:
    selector = _ProofSelector((t.resource for t in assessment.compliant), citations)
    for treatment in assessment.compliant:
        resource = treatment.resource
        proof = selector.proof(resource)
        if proof:
            plan.add(treatment.plan.verification, resource, proof)
        else:
            logger.warning(
                "%s: no citable evidence for %s/%s; verification skipped",
                assessment.case_id,
                resource.system_id,
                resource.resource_id,
            )


def _plan_fixed(
    plan: _Plan,
    assessment: Assessment,
    resources: Iterable[Resource],
    citations: Citations | None,
) -> None:
    """Add the account/security verifications when the assessment lacks them."""
    judged = [t.resource for t in (*assessment.violations, *assessment.compliant)]
    judged_systems = {r.system_id for r in judged}
    judged_keys = {r.key for r in judged}
    complete = assessment.status is DeletionStatus.COMPLETE
    for system_id, operation in FIXED_VERIFICATIONS:
        if system_id in judged_systems:
            continue
        unjudged = sorted(
            (
                r
                for r in resources
                if r.system_id == system_id and r.key not in judged_keys
            ),
            key=lambda r: r.resource_id,
        )
        selector = _ProofSelector([*judged, *unjudged], citations)
        for resource in unjudged:
            if not complete:
                plan.add(operation, resource, "")
                continue
            proof = selector.proof(resource)
            if proof:
                plan.add(operation, resource, proof)
            else:
                logger.warning(
                    "%s: no citable evidence for %s/%s; fixed step skipped",
                    assessment.case_id,
                    system_id,
                    resource.resource_id,
                )


def build_steps(
    assessment: Assessment,
    *,
    resources: Sequence[Resource] = (),
    citations: Citations | None = None,
) -> list[Step]:
    """Ordered, numbered and validated steps for ``assessment``.

    ``resources`` are the investigated resources of the case; they are only
    used to recover the fixed account/security steps (see module docstring).
    ``citations`` restricts and ranks the cited evidence (``None`` accepts
    every id the resources carry). Raises ``models.InvalidStepError`` when the
    result breaks the contract.
    """
    if assessment.status is DeletionStatus.NOT_APPLICABLE:
        return []
    plan = _Plan()
    if assessment.status is DeletionStatus.INCOMPLETE:
        if not assessment.violations:
            logger.warning(
                "%s: INCOMPLETE without a groundable finding; empty plan",
                assessment.case_id,
            )
            return []
        _plan_incomplete(plan, assessment, citations)
    else:
        _plan_complete(plan, assessment, citations)
    _plan_fixed(plan, assessment, resources, citations)
    steps = plan.steps()
    if not steps:
        logger.warning(
            "%s: %s but no step could be grounded in visible evidence; empty plan",
            assessment.case_id,
            assessment.status.value,
        )
        return []
    validate_plan(assessment.status, steps)
    return steps


def validate_plan(status: DeletionStatus, steps: Sequence[Step]) -> None:
    """``models.validate_steps`` that tolerates an empty plan only for ``INCOMPLETE``.

    An incomplete case whose records could not be grounded in visible data has
    nothing to cite and ids are never invented, so ``[]`` is the honest answer
    (reported as a warning by ``build_steps``). ``COMPLETE`` with no step is a
    contract violation: a completed deletion must be proven by evidence.
    """
    if not steps and status is DeletionStatus.INCOMPLETE:
        return
    validate_steps(status, steps)


__all__ = (
    "Citations",
    "FIXED_VERIFICATIONS",
    "build_steps",
    "validate_plan",
    "visible_citations",
)
