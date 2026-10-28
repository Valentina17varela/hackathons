"""Contracts and domain models for the Ghost Data Still Exists deletion-audit agent.

This module defines the closed vocabulary of the output contract (phases,
operations, states, statuses), the canonical ordering used to serialize
``steps_json``, the mapping from policy actions to finding/remediation/
verification operations, and the dataclasses shared by every pipeline stage.

A ``Step`` validates itself on construction, so an invalid
phase/operation/state combination cannot be built.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class DeletionStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReportedStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIALLY_COMPLETE = "PARTIALLY_COMPLETE"
    NOT_STARTED = "NOT_STARTED"


class Phase(str, Enum):
    FINDING = "FINDING"
    REMEDIATION = "REMEDIATION"
    VERIFICATION = "VERIFICATION"


class Operation(str, Enum):
    # Findings
    REPORT_REMAINING_RECORD = "REPORT_REMAINING_RECORD"
    REPORT_EXPOSED_PII = "REPORT_EXPOSED_PII"
    REPORT_ACTIVE_DATA_FLOW = "REPORT_ACTIVE_DATA_FLOW"
    REPORT_RESTORE_RISK = "REPORT_RESTORE_RISK"
    # Remediation
    DISABLE_DATA_FLOW = "DISABLE_DATA_FLOW"
    BLOCK_RESTORE = "BLOCK_RESTORE"
    DELETE_RECORD = "DELETE_RECORD"
    REDACT_RECORD = "REDACT_RECORD"
    ANONYMIZE_RECORD = "ANONYMIZE_RECORD"
    # Verification
    VERIFY_RECORD_ABSENT = "VERIFY_RECORD_ABSENT"
    VERIFY_RECORD_PRESENT = "VERIFY_RECORD_PRESENT"
    VERIFY_RECORD_REDACTED = "VERIFY_RECORD_REDACTED"
    VERIFY_RECORD_ANONYMIZED = "VERIFY_RECORD_ANONYMIZED"
    VERIFY_DATA_FLOW_DISABLED = "VERIFY_DATA_FLOW_DISABLED"
    VERIFY_RESTORE_BLOCKED = "VERIFY_RESTORE_BLOCKED"


class StepState(str, Enum):
    PRESENT = "PRESENT"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"
    ABSENT = "ABSENT"
    REDACTED = "REDACTED"
    ANONYMIZED = "ANONYMIZED"


class RuleAction(str, Enum):
    """``required_action`` values in ``deletion_rules.csv``."""

    DELETE = "DELETE"
    REDACT = "REDACT"
    ANONYMIZE = "ANONYMIZE"
    RETAIN = "RETAIN"


class RiskKind(str, Enum):
    """Recreation risks that are not records themselves."""

    DATA_FLOW = "DATA_FLOW"
    RESTORE = "RESTORE"


class ObservedState(str, Enum):
    """Reconstructed state of a resource at ``audit_at``."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    REDACTED = "REDACTED"
    ANONYMIZED = "ANONYMIZED"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Canonical contract tables
# ---------------------------------------------------------------------------

PHASE_ORDER: tuple[Phase, ...] = (Phase.FINDING, Phase.REMEDIATION, Phase.VERIFICATION)
PHASE_RANK: Mapping[Phase, int] = {phase: i for i, phase in enumerate(PHASE_ORDER)}


@dataclass(frozen=True, slots=True)
class OperationSpec:
    phase: Phase
    state: StepState


# Exactly the allowed (operation -> phase, state) combinations, in the
# canonical order of the problem statement's "Allowed Operations" table.
OPERATION_SPECS: Mapping[Operation, OperationSpec] = {
    Operation.REPORT_REMAINING_RECORD: OperationSpec(Phase.FINDING, StepState.PRESENT),
    Operation.REPORT_EXPOSED_PII: OperationSpec(Phase.FINDING, StepState.PRESENT),
    Operation.REPORT_ACTIVE_DATA_FLOW: OperationSpec(Phase.FINDING, StepState.ACTIVE),
    Operation.REPORT_RESTORE_RISK: OperationSpec(Phase.FINDING, StepState.ACTIVE),
    Operation.DISABLE_DATA_FLOW: OperationSpec(Phase.REMEDIATION, StepState.DISABLED),
    Operation.BLOCK_RESTORE: OperationSpec(Phase.REMEDIATION, StepState.BLOCKED),
    Operation.DELETE_RECORD: OperationSpec(Phase.REMEDIATION, StepState.ABSENT),
    Operation.REDACT_RECORD: OperationSpec(Phase.REMEDIATION, StepState.REDACTED),
    Operation.ANONYMIZE_RECORD: OperationSpec(Phase.REMEDIATION, StepState.ANONYMIZED),
    Operation.VERIFY_RECORD_ABSENT: OperationSpec(Phase.VERIFICATION, StepState.ABSENT),
    Operation.VERIFY_RECORD_PRESENT: OperationSpec(
        Phase.VERIFICATION, StepState.PRESENT
    ),
    Operation.VERIFY_RECORD_REDACTED: OperationSpec(
        Phase.VERIFICATION, StepState.REDACTED
    ),
    Operation.VERIFY_RECORD_ANONYMIZED: OperationSpec(
        Phase.VERIFICATION, StepState.ANONYMIZED
    ),
    Operation.VERIFY_DATA_FLOW_DISABLED: OperationSpec(
        Phase.VERIFICATION, StepState.DISABLED
    ),
    Operation.VERIFY_RESTORE_BLOCKED: OperationSpec(
        Phase.VERIFICATION, StepState.BLOCKED
    ),
}

OPERATION_ORDER: tuple[Operation, ...] = tuple(OPERATION_SPECS)
OPERATION_RANK: Mapping[Operation, int] = {
    op: i for i, op in enumerate(OPERATION_ORDER)
}
OPERATION_STATE: Mapping[Operation, StepState] = {
    op: s.state for op, s in OPERATION_SPECS.items()
}
OPERATION_PHASE: Mapping[Operation, Phase] = {
    op: s.phase for op, s in OPERATION_SPECS.items()
}


@dataclass(frozen=True, slots=True)
class TreatmentPlan:
    """Operations used for one policy action or risk kind.

    ``finding``/``remediation`` are ``None`` when the treatment never produces
    a violation to remediate (e.g. ``RETAIN`` only needs a presence check).
    """

    finding: Operation | None
    remediation: Operation | None
    verification: Operation


RULE_ACTION_PLAN: Mapping[RuleAction, TreatmentPlan] = {
    RuleAction.DELETE: TreatmentPlan(
        Operation.REPORT_REMAINING_RECORD,
        Operation.DELETE_RECORD,
        Operation.VERIFY_RECORD_ABSENT,
    ),
    RuleAction.REDACT: TreatmentPlan(
        Operation.REPORT_EXPOSED_PII,
        Operation.REDACT_RECORD,
        Operation.VERIFY_RECORD_REDACTED,
    ),
    RuleAction.ANONYMIZE: TreatmentPlan(
        Operation.REPORT_EXPOSED_PII,
        Operation.ANONYMIZE_RECORD,
        Operation.VERIFY_RECORD_ANONYMIZED,
    ),
    RuleAction.RETAIN: TreatmentPlan(None, None, Operation.VERIFY_RECORD_PRESENT),
}

RISK_PLAN: Mapping[RiskKind, TreatmentPlan] = {
    RiskKind.DATA_FLOW: TreatmentPlan(
        Operation.REPORT_ACTIVE_DATA_FLOW,
        Operation.DISABLE_DATA_FLOW,
        Operation.VERIFY_DATA_FLOW_DISABLED,
    ),
    RiskKind.RESTORE: TreatmentPlan(
        Operation.REPORT_RESTORE_RISK,
        Operation.BLOCK_RESTORE,
        Operation.VERIFY_RESTORE_BLOCKED,
    ),
}

# Observed state that satisfies each policy action (used to decide compliance).
RULE_ACTION_COMPLIANT_STATE: Mapping[RuleAction, ObservedState] = {
    RuleAction.DELETE: ObservedState.ABSENT,
    RuleAction.REDACT: ObservedState.REDACTED,
    RuleAction.ANONYMIZE: ObservedState.ANONYMIZED,
    RuleAction.RETAIN: ObservedState.PRESENT,
}

RISK_COMPLIANT_STATE: Mapping[RiskKind, ObservedState] = {
    RiskKind.DATA_FLOW: ObservedState.DISABLED,
    RiskKind.RESTORE: ObservedState.BLOCKED,
}

TOMBSTONE_CATEGORY = "ACCOUNT_TOMBSTONE"
STEP_FIELDS: tuple[str, ...] = (
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
)


# ---------------------------------------------------------------------------
# Output step
# ---------------------------------------------------------------------------


class InvalidStepError(ValueError):
    """Raised when a step or step sequence violates the output contract."""


@dataclass(frozen=True, slots=True)
class Step:
    """One element of ``steps_json``; validated on construction."""

    step_no: int
    phase: Phase
    system_id: str
    resource_id: str
    operation: Operation
    state: StepState
    evidence_id: str = ""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "phase", Phase(self.phase))
            object.__setattr__(self, "operation", Operation(self.operation))
            object.__setattr__(self, "state", StepState(self.state))
        except ValueError as exc:
            raise InvalidStepError(str(exc)) from exc
        if (
            isinstance(self.step_no, bool)
            or not isinstance(self.step_no, int)
            or self.step_no < 1
        ):
            raise InvalidStepError(
                f"step_no must be a positive int, got {self.step_no!r}"
            )
        for name in ("system_id", "resource_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidStepError(f"{name} must be a non-empty string")
        if not isinstance(self.evidence_id, str):
            raise InvalidStepError("evidence_id must be a string")
        spec = OPERATION_SPECS[self.operation]
        if spec.phase is not self.phase or spec.state is not self.state:
            raise InvalidStepError(
                f"{self.operation.value} requires phase={spec.phase.value} "
                f"state={spec.state.value}, got phase={self.phase.value} state={self.state.value}"
            )
        if self.phase is Phase.FINDING and not self.evidence_id:
            raise InvalidStepError("FINDING steps must cite an evidence_id")
        if self.phase is Phase.REMEDIATION and self.evidence_id:
            raise InvalidStepError("REMEDIATION steps must use an empty evidence_id")

    @classmethod
    def create(
        cls,
        step_no: int,
        system_id: str,
        resource_id: str,
        operation: Operation,
        evidence_id: str = "",
    ) -> Step:
        """Build a step deriving phase and state from the operation."""
        spec = OPERATION_SPECS[Operation(operation)]
        return cls(
            step_no,
            spec.phase,
            system_id,
            resource_id,
            Operation(operation),
            spec.state,
            evidence_id,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Step:
        if set(data) != set(STEP_FIELDS):
            raise InvalidStepError(
                f"step must have exactly fields {STEP_FIELDS}, got {sorted(data)}"
            )
        return cls(**{name: data[name] for name in STEP_FIELDS})

    def sort_key(self) -> tuple[int, int, str, str]:
        """Canonical order: phase, operation table order, system_id, resource_id."""
        return (
            PHASE_RANK[self.phase],
            OPERATION_RANK[self.operation],
            self.system_id,
            self.resource_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_no": self.step_no,
            "phase": self.phase.value,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation.value,
            "state": self.state.value,
            "evidence_id": self.evidence_id,
        }


def validate_steps(status: DeletionStatus, steps: Sequence[Step]) -> None:
    """Validate a full ``steps_json`` sequence for a case status."""
    status = DeletionStatus(status)
    if status is DeletionStatus.NOT_APPLICABLE:
        if steps:
            raise InvalidStepError("NOT_APPLICABLE cases must have no steps")
        return
    if not steps:
        raise InvalidStepError(f"{status.value} cases must have at least one step")
    for expected_no, step in enumerate(steps, start=1):
        if step.step_no != expected_no:
            raise InvalidStepError(
                f"step_no must be consecutive from 1; expected {expected_no}, got {step.step_no}"
            )
    keys = [step.sort_key() for step in steps]
    if keys != sorted(keys):
        raise InvalidStepError(
            "steps are not in canonical phase/operation/system_id/resource_id order"
        )
    if len(set(keys)) != len(keys):
        raise InvalidStepError("duplicate steps detected")
    if status is DeletionStatus.COMPLETE:
        for step in steps:
            if step.phase is not Phase.VERIFICATION:
                raise InvalidStepError(
                    "COMPLETE cases may only contain VERIFICATION steps"
                )
            if not step.evidence_id:
                raise InvalidStepError(
                    "COMPLETE verification steps must cite an evidence_id"
                )
    else:
        if not any(step.phase is Phase.FINDING for step in steps):
            raise InvalidStepError("INCOMPLETE cases must contain at least one FINDING")
        for step in steps:
            if step.phase is Phase.VERIFICATION and step.evidence_id:
                raise InvalidStepError(
                    "INCOMPLETE verification steps must use an empty evidence_id"
                )


def number_steps(steps: Iterable[Step]) -> list[Step]:
    """Sort steps canonically and renumber them consecutively from 1."""
    ordered = sorted(steps, key=Step.sort_key)
    return [
        Step(
            i, s.phase, s.system_id, s.resource_id, s.operation, s.state, s.evidence_id
        )
        for i, s in enumerate(ordered, start=1)
    ]


def serialize_steps(steps: Sequence[Step]) -> str:
    """Deterministic compact JSON for the ``steps_json`` column."""
    return json.dumps(
        [s.to_dict() for s in steps], ensure_ascii=False, separators=(",", ":")
    )


# ---------------------------------------------------------------------------
# Domain dataclasses shared across pipeline stages
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    email: str
    requested_at: datetime
    reported_status: ReportedStatus
    reported_at: datetime
    audit_at: datetime
    request_text: str


@dataclass(frozen=True, slots=True)
class DeletionRule:
    rule_id: str
    data_category: str
    condition: str
    required_action: RuleAction

    @property
    def plan(self) -> TreatmentPlan:
        return RULE_ACTION_PLAN[self.required_action]


@dataclass(slots=True)
class Identity:
    """Current and historical identifiers of one subject as of ``audit_at``."""

    user_id: str
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    email_hashes: set[str] = field(default_factory=set)
    audit_ids: list[str] = field(default_factory=list)

    def all_identifiers(self) -> set[str]:
        return {
            self.user_id,
            *self.emails,
            *self.phones,
            *self.names,
            *self.email_hashes,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """A normalized observation extracted from one evidence source."""

    evidence_id: str  # file_id, activity_id or audit_id
    captured_at: datetime
    system_id: str
    resource_id: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    source_path: str = ""


@dataclass(slots=True)
class Resource:
    """A record or recreation risk linked to the subject, with its state at ``audit_at``."""

    system_id: str
    resource_id: str
    data_category: str | None = None
    risk_kind: RiskKind | None = None
    observed_state: ObservedState = ObservedState.UNKNOWN
    evidence_id: str = ""
    depends_on: tuple[str, ...] = ()  # resource_ids of upstream sources

    @property
    def key(self) -> tuple[str, str]:
        return (self.system_id, self.resource_id)


@dataclass(frozen=True, slots=True)
class Treatment:
    """A resource paired with the treatment required by policy or risk kind."""

    resource: Resource
    plan: TreatmentPlan
    compliant_state: ObservedState
    action: RuleAction | None = None

    @property
    def is_compliant(self) -> bool:
        return self.resource.observed_state is self.compliant_state


@dataclass(slots=True)
class Assessment:
    case_id: str
    status: DeletionStatus
    violations: list[Treatment] = field(default_factory=list)
    compliant: list[Treatment] = field(default_factory=list)
