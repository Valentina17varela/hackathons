"""Accuracy metrics that compare predicted answers with reference answers.

Four views of the same comparison are computed per case and aggregated:

* **Status accuracy** -- the predicted ``deletion_status`` equals the reference.
* **Step accuracy** -- steps are identified by ``(phase, operation, system_id,
  resource_id)`` and compared as multisets (precision / recall / F1). A *strict*
  variant also requires the same ``evidence_id``.
* **Field accuracy** -- steps are compared position by position (step ``i`` of
  the prediction against step ``i`` of the reference); each of the seven step
  fields is scored separately. Missing and surplus positions count as wrong for
  every field, so one missing step also penalizes the steps that follow it.
* **Sequence accuracy** -- the ordered list of step identities equals the
  reference exactly (``sequence_exact``), plus a softer similarity
  ``2 * LCS / (len(expected) + len(predicted))`` that tolerates local omissions.

An *exact row* is a case whose status and complete step list (including
``step_no`` and ``evidence_id``) equal the reference.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from models import STEP_FIELDS, DeletionStatus, Step

StepKey = tuple[str, str, str, str]


def step_key(step: Step) -> StepKey:
    """Identity of a step: what is done, to which resource, in which phase."""
    return (step.phase.value, step.operation.value, step.system_id, step.resource_id)


def longest_common_subsequence(a: Sequence[StepKey], b: Sequence[StepKey]) -> int:
    """Length of the longest common subsequence of two key sequences."""
    previous = [0] * (len(b) + 1)
    for item_a in a:
        current = [0]
        for j, item_b in enumerate(b, start=1):
            if item_a == item_b:
                current.append(previous[j - 1] + 1)
            else:
                current.append(max(previous[j], current[j - 1]))
        previous = current
    return previous[-1]


@dataclass(frozen=True, slots=True)
class CaseScore:
    """Comparison of one predicted answer with its reference."""

    case_id: str
    expected_status: DeletionStatus
    predicted_status: DeletionStatus
    expected_steps: tuple[Step, ...]
    predicted_steps: tuple[Step, ...]

    @property
    def status_match(self) -> bool:
        return self.expected_status is self.predicted_status

    @property
    def exact_row(self) -> bool:
        return self.status_match and self.expected_steps == self.predicted_steps

    @property
    def expected_keys(self) -> list[StepKey]:
        return [step_key(s) for s in self.expected_steps]

    @property
    def predicted_keys(self) -> list[StepKey]:
        return [step_key(s) for s in self.predicted_steps]

    @property
    def matched_steps(self) -> int:
        common = Counter(self.expected_keys) & Counter(self.predicted_keys)
        return sum(common.values())

    @property
    def strict_matched_steps(self) -> int:
        expected = Counter((step_key(s), s.evidence_id) for s in self.expected_steps)
        predicted = Counter((step_key(s), s.evidence_id) for s in self.predicted_steps)
        return sum((expected & predicted).values())

    @property
    def missing_keys(self) -> list[StepKey]:
        """Reference steps the prediction does not contain."""
        diff = Counter(self.expected_keys) - Counter(self.predicted_keys)
        return sorted(diff.elements())

    @property
    def extra_keys(self) -> list[StepKey]:
        """Predicted steps the reference does not contain."""
        diff = Counter(self.predicted_keys) - Counter(self.expected_keys)
        return sorted(diff.elements())

    @property
    def sequence_exact(self) -> bool:
        return self.expected_keys == self.predicted_keys

    @property
    def sequence_similarity(self) -> float:
        total = len(self.expected_steps) + len(self.predicted_steps)
        if total == 0:
            return 1.0
        lcs = longest_common_subsequence(self.expected_keys, self.predicted_keys)
        return 2 * lcs / total

    @property
    def positions(self) -> int:
        return max(len(self.expected_steps), len(self.predicted_steps))

    def field_hits(self) -> dict[str, int]:
        """Number of positions where each step field equals the reference."""
        hits = dict.fromkeys(STEP_FIELDS, 0)
        for expected, predicted in zip(self.expected_steps, self.predicted_steps):
            for name in STEP_FIELDS:
                if getattr(expected, name) == getattr(predicted, name):
                    hits[name] += 1
        return hits


@dataclass(frozen=True, slots=True)
class Summary:
    """Aggregate accuracy over a set of scored cases."""

    cases: int
    status_correct: int
    exact_rows: int
    expected_steps: int
    predicted_steps: int
    matched_steps: int
    strict_matched_steps: int
    sequence_exact_cases: int
    sequence_similarity_mean: float
    field_hits: dict[str, int]
    field_positions: int

    @staticmethod
    def _ratio(numerator: float, denominator: float) -> float:
        return numerator / denominator if denominator else 1.0

    @property
    def status_accuracy(self) -> float:
        return self._ratio(self.status_correct, self.cases)

    @property
    def row_accuracy(self) -> float:
        return self._ratio(self.exact_rows, self.cases)

    @property
    def step_precision(self) -> float:
        return self._ratio(self.matched_steps, self.predicted_steps)

    @property
    def step_recall(self) -> float:
        return self._ratio(self.matched_steps, self.expected_steps)

    @property
    def step_f1(self) -> float:
        p, r = self.step_precision, self.step_recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def strict_step_precision(self) -> float:
        return self._ratio(self.strict_matched_steps, self.predicted_steps)

    @property
    def strict_step_recall(self) -> float:
        return self._ratio(self.strict_matched_steps, self.expected_steps)

    @property
    def sequence_accuracy(self) -> float:
        return self._ratio(self.sequence_exact_cases, self.cases)

    def field_accuracy(self, name: str) -> float:
        return self._ratio(self.field_hits[name], self.field_positions)

    @property
    def overall_field_accuracy(self) -> float:
        return self._ratio(
            sum(self.field_hits.values()), self.field_positions * len(STEP_FIELDS)
        )


def summarize(scores: Sequence[CaseScore]) -> Summary:
    """Aggregate per-case scores (micro-averaged over steps and positions)."""
    field_hits = dict.fromkeys(STEP_FIELDS, 0)
    for score in scores:
        for name, count in score.field_hits().items():
            field_hits[name] += count
    similarity = sum(s.sequence_similarity for s in scores)
    return Summary(
        cases=len(scores),
        status_correct=sum(s.status_match for s in scores),
        exact_rows=sum(s.exact_row for s in scores),
        expected_steps=sum(len(s.expected_steps) for s in scores),
        predicted_steps=sum(len(s.predicted_steps) for s in scores),
        matched_steps=sum(s.matched_steps for s in scores),
        strict_matched_steps=sum(s.strict_matched_steps for s in scores),
        sequence_exact_cases=sum(s.sequence_exact for s in scores),
        sequence_similarity_mean=similarity / len(scores) if scores else 1.0,
        field_hits=field_hits,
        field_positions=sum(s.positions for s in scores),
    )
