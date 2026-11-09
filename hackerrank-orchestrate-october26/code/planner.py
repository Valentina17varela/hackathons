"""Step planner (TASK-09).

Turns an ``Assessment`` into the ordered ``steps_json`` of one case.

Plan shape
----------
* ``NOT_APPLICABLE``: no steps.
* ``INCOMPLETE``: one ``FINDING`` per violation that has a finding operation
  (cited with participant-visible evidence), one ``REMEDIATION`` per violation
  (empty ``evidence_id``), one ``VERIFICATION`` per judged resource, whether
  it was a violation or already compliant (empty ``evidence_id``: the check is
  future work), and one ``VERIFICATION`` per *unjudged* resource from the
  policy-driven checklist below.
* ``COMPLETE``: only ``VERIFICATION`` steps, each citing evidence that proves
  the current compliant state.

Ordering
--------
Steps are sorted by the canonical key of the output contract: phase, operation
(table order), ``system_id``, ``resource_id`` (``models.number_steps``). The
operation table already puts ``DISABLE_DATA_FLOW`` and ``BLOCK_RESTORE`` before
every record change, so recreation paths are closed before dependent records
are touched. Records of different systems that share an operation follow the
contract's ``system_id`` order; the public samples confirm that the contract
order, not source-before/after-copy order, is expected. The dependency rules
that this ordering must satisfy are not assumed, they are *enforced* on the
finished plan (``enforce_dependencies``).

Evidence
--------
Only evidence at or before ``audit_at`` may be cited (``Citations``). A step
that needs evidence and has none is skipped; ids are never invented.

A citation on a ``COMPLETE`` case (and on any step that must prove a state)
may only use an id from ``state_evidence_ids``: an observation that
establishes the final state itself. An id that only proves the *linking* of a
resource never proves its state, so it is not accepted as proof of
completeness. When a resource is supported by several ids the choice is
deterministic:

1. the evidence file that supports most of the cited resources of the case
   (at least two), so one capture of the case is cited consistently;
2. else the investigator's primary proof when it is an evidence file;
3. else the latest-captured evidence file, then the latest activity or audit
   event.

Policy-driven checklist
-----------------------
A remediation plan is only finished when the checks that policy requires are
listed, wherever they are relevant: the subject's account absence, the
anonymization of analytics profiles, the presence of retained records
(security audit logs, legal holds) and the closure of every flow and restore
path. Judged resources get their verification from the treatment; for every
*unjudged* resource (unknown state, or no rule whose condition applies) the
checklist still emits the verification operation that its category's rule (or
risk kind) requires, with an empty ``evidence_id`` (future work). A resource
whose category has no rule at all is skipped, never invented. The account
(``VERIFY_RECORD_ABSENT``) and security-audit (``VERIFY_RECORD_PRESENT``)
checks remain the mandatory minimum for every deletion case.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from investigator import ACCOUNT, SECURITY
from models import (
    Assessment,
    DeletionRule,
    DeletionStatus,
    Operation,
    Phase,
    Resource,
    RiskKind,
    RISK_PLAN,
    Step,
    InvalidStepError,
    number_steps,
    validate_steps,
)
from parser import Tables

logger = logging.getLogger(__name__)

# Mandatory minimum checks for every deletion case, used when a resource of
# these systems was found but could not be judged (no rule, unknown state):
# the account record must be absent and the security audit log must remain.
FIXED_VERIFICATIONS: Mapping[str, Operation] = {
    ACCOUNT: Operation.VERIFY_RECORD_ABSENT,
    SECURITY: Operation.VERIFY_RECORD_PRESENT,
}

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


def _candidates(
    resource: Resource, citations: Citations | None, state_only: bool = False
) -> list[str]:
    """Distinct citable ids supporting ``resource``; the primary proof comes first.

    ``state_only`` restricts the ids to those that establish the resource's
    final state (``state_evidence_ids``): the only ids that may back a step
    which claims to prove a state, such as every verification of a ``COMPLETE``
    case. Linking-only ids then never pass as proof of deletion completeness.
    """
    if state_only:
        raw = (resource.evidence_id, *getattr(resource, "state_evidence_ids", ()))
    else:
        raw = (resource.evidence_id, *getattr(resource, "evidence_ids", ()))
    seen: dict[str, None] = {}
    for evidence_id in raw:
        if evidence_id and (citations is None or evidence_id in citations):
            seen.setdefault(evidence_id, None)
    return list(seen)


class _ProofSelector:
    """Chooses the evidence id cited for each resource of one case.

    ``state_only`` builds the selector over state-proving ids only (see
    ``_candidates``).
    """

    def __init__(
        self,
        cited: Iterable[Resource],
        citations: Citations | None,
        state_only: bool = False,
    ) -> None:
        self._citations = citations
        self._state_only = state_only
        self._shared: str | None = None
        if citations is None:
            return
        support: Counter[str] = Counter(
            evidence_id
            for resource in cited
            for evidence_id in _candidates(resource, citations, state_only)
            if citations.is_file(evidence_id)
        )
        shared = [e for e, n in support.items() if n >= _MIN_SHARED_SUPPORT]
        if shared:
            self._shared = min(shared, key=lambda e: (-support[e], citations.rank(e)))

    def proof(self, resource: Resource) -> str:
        candidates = _candidates(resource, self._citations, self._state_only)
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
    # Only ids that establish the compliant state may prove completeness.
    selector = _ProofSelector(
        (t.resource for t in assessment.compliant), citations, state_only=True
    )
    for treatment in assessment.compliant:
        resource = treatment.resource
        proof = selector.proof(resource)
        if proof:
            plan.add(treatment.plan.verification, resource, proof)
        else:
            logger.warning(
                "%s: no citable state-proving evidence for %s/%s; verification "
                "skipped (completeness cannot be proven by a linking-only id)",
                assessment.case_id,
                resource.system_id,
                resource.resource_id,
            )


def _checklist_operation(
    resource: Resource, rules: Mapping[str, Sequence[DeletionRule]] | None
) -> Operation | None:
    """The verification operation policy requires for an unjudged resource.

    The operation comes from the resource's risk kind or from the rule whose
    condition applies to its category; the account and security-audit checks
    are the mandatory minimum. ``None`` when nothing grounded defines one.
    """
    if resource.risk_kind is not None:
        return RISK_PLAN[resource.risk_kind].verification
    if rules:
        from evaluator import select_rule  # local import: keep modules decoupled

        rule = select_rule(resource, rules)
        if rule is not None:
            return rule.plan.verification
    return FIXED_VERIFICATIONS.get(resource.system_id)


def _plan_checklist(
    plan: _Plan,
    assessment: Assessment,
    resources: Iterable[Resource],
    citations: Citations | None,
    rules: Mapping[str, Sequence[DeletionRule]] | None = None,
) -> None:
    """Emit the policy-required verification for every unjudged resource.

    Judged resources already carry their verification from the treatment.
    An unjudged resource (unknown state, or no applicable rule) still gets the
    check that its category requires — as future work with an empty
    ``evidence_id`` on an ``INCOMPLETE`` case, or proven by state evidence on a
    ``COMPLETE`` case (an unjudged resource has no state proof, so on a
    ``COMPLETE`` case the step is skipped and the case is re-examined by the
    caller). Ids are never invented: a resource whose category has no rule and
    no fixed check is skipped with a log line.
    """
    judged_keys = {t.resource.key for t in (*assessment.violations, *assessment.compliant)}
    complete = assessment.status is DeletionStatus.COMPLETE
    unjudged = sorted(
        (r for r in resources if r.key not in judged_keys),
        key=lambda r: (r.system_id, r.resource_id),
    )
    selector = _ProofSelector(
        [t.resource for t in (*assessment.violations, *assessment.compliant)],
        citations,
        state_only=True,
    )
    for resource in unjudged:
        operation = _checklist_operation(resource, rules)
        if operation is None:
            logger.debug(
                "%s: no policy check defined for unjudged %s/%s; skipped",
                assessment.case_id,
                resource.system_id,
                resource.resource_id,
            )
            continue
        if not complete:
            plan.add(operation, resource, "")
            continue
        proof = selector.proof(resource)
        if proof:
            plan.add(operation, resource, proof)
        else:
            logger.warning(
                "%s: unjudged %s/%s has no state-proving evidence; checklist "
                "step skipped",
                assessment.case_id,
                resource.system_id,
                resource.resource_id,
            )


def build_steps(
    assessment: Assessment,
    *,
    resources: Sequence[Resource] = (),
    citations: Citations | None = None,
    rules: Mapping[str, Sequence[DeletionRule]] | None = None,
) -> list[Step]:
    """Ordered, numbered and validated steps for ``assessment``.

    ``resources`` are the investigated resources of the case; they feed the
    policy-driven checklist for unjudged resources and the dependency checks.
    ``citations`` restricts and ranks the cited evidence (``None`` accepts
    every id the resources carry). ``rules`` (``evaluator.rules_by_category``)
    drives the checklist; without it only the fixed account/security checks
    apply. Raises ``models.InvalidStepError`` when the result breaks the
    contract or a dependency rule.
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
    _plan_checklist(plan, assessment, resources, citations, rules)
    steps = plan.steps()
    if not steps:
        logger.warning(
            "%s: %s but no step could be grounded in visible evidence; empty plan",
            assessment.case_id,
            assessment.status.value,
        )
        return []
    validate_plan(assessment.status, steps)
    enforce_dependencies(steps, resources)
    return steps


# ---------------------------------------------------------------------------
# Dependency enforcement
# ---------------------------------------------------------------------------


def enforce_dependencies(steps: Sequence[Step], resources: Sequence[Resource]) -> None:
    """Enforce the contract's remediation dependency rules on a finished plan.

    Two rules, both checked on the finished plan instead of being assumed from
    the canonical sort:

    * A data flow or backup that can copy or restore a record must be disabled
      or blocked *before* any record it feeds is remediated (recreation paths
      are closed first). A flow's ``depends_on`` names the records it feeds.
    * A downstream copy is processed *before* its source record. A record's
      ``depends_on`` names its upstream sources, so the record's own step must
      come first.

    A violation of the first rule raises ``InvalidStepError``: it would let a
    live flow recreate a record that was just remediated. A conflict of the
    second rule with the canonical contract order is reported as a warning,
    because the output contract fixes the step order and it cannot be silently
    reordered.
    """
    remediation: dict[tuple[str, str], int] = {
        (s.system_id, s.resource_id): i
        for i, s in enumerate(steps)
        if s.phase is Phase.REMEDIATION
    }
    if not remediation:
        return
    by_key = {r.key: r for r in resources}
    # Rule 1 — close every flow/backup before the records it feeds.
    for resource in resources:
        if resource.risk_kind is None or not resource.depends_on:
            continue
        flow_index = remediation.get(resource.key)
        if flow_index is None:
            continue
        for downstream in resource.depends_on:
            downstream_key = _resource_key(downstream)
            if downstream_key is None:
                continue
            record_index = remediation.get(downstream_key)
            if record_index is not None and record_index < flow_index:
                step = steps[record_index]
                raise InvalidStepError(
                    f"{downstream_key[0]}/{downstream_key[1]} "
                    f"({step.operation.value}) is remediated before "
                    f"{resource.system_id}/{resource.resource_id} "
                    f"({steps[flow_index].operation.value}), which can copy or "
                    "restore the record back"
                )
    # Rule 2 — downstream copies before their source records.
    for key, index in remediation.items():
        resource = by_key.get(key)
        if resource is None or resource.risk_kind is not None:
            continue
        for upstream in resource.depends_on:
            upstream_key = _resource_key(upstream)
            if upstream_key is None or upstream_key not in remediation:
                continue
            upstream_resource = by_key.get(upstream_key)
            if upstream_resource is None or upstream_resource.risk_kind is not None:
                continue  # a flow/backup source is handled by rule 1
            if remediation[upstream_key] < index:
                logger.warning(
                    "canonical order remediated source %s/%s before its "
                    "downstream copy %s/%s; the contract fixes the step order",
                    upstream_key[0],
                    upstream_key[1],
                    key[0],
                    key[1],
                )


def _resource_key(resource_id: str) -> tuple[str, str] | None:
    """``(system_id, resource_id)`` for a dependency id, or ``None``."""
    from evidence_extractors import system_for_resource

    system_id = system_for_resource(resource_id)
    return (system_id, resource_id) if system_id else None


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
