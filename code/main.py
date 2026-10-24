"""Command-line entry point: audit every deletion request and write ``output.csv``.

Usage (from the repository root)::

    python3 code/main.py                          # 518 evaluation cases -> output.csv
    python3 code/main.py --cases sample --output /tmp/sample_run.csv

Pipeline per case (see ``tasks.md``)::

    intent gate -> identity resolution -> investigation -> policy assessment -> step planner

The output file is validated against the output contract (one row per input
case, sorted, valid ``steps_json``) before anything is written, so a failed
run never leaves a partial or invalid ``output.csv`` behind.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from evaluator import IntentClassifier, assess
from evidence_extractors import EvidenceIndex
from identity_resolver import AmbiguousIdentityError, IdentityResolver
from investigator import Investigator
from llm import LLMClient
from models import (
    Case,
    DeletionStatus,
    InvalidStepError,
    Step,
    serialize_steps,
)
from parser import (
    DEFAULT_DATASET_DIR,
    REPO_ROOT,
    Tables,
    build_evidence_index,
    extract_multimodal,
    extract_structured,
    load_tables,
)
from planner import build_steps, validate_plan, visible_citations

logger = logging.getLogger("main")

OUTPUT_COLUMNS: tuple[str, ...] = ("case_id", "deletion_status", "steps_json")
DEFAULT_OUTPUT_PATH = REPO_ROOT / "output.csv"


class OutputValidationError(ValueError):
    """Raised when the rows to write violate the output contract."""


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Answer for one case: the status and its ordered steps."""

    case_id: str
    status: DeletionStatus
    steps: tuple[Step, ...] = ()

    def to_row(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "deletion_status": self.status.value,
            "steps_json": serialize_steps(self.steps),
        }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class AuditPipeline:
    """Runs the full audit of single cases against one loaded dataset."""

    def __init__(
        self,
        tables: Tables,
        index: EvidenceIndex,
        classifier: IntentClassifier,
    ) -> None:
        self._tables = tables
        self._classifier = classifier
        self._resolver = IdentityResolver(tables)
        self._investigator = Investigator(tables, index)

    def run_case(self, case: Case) -> CaseResult:
        if not self._classifier.classify(case.request_text).authorizes_deletion:
            return CaseResult(case.case_id, DeletionStatus.NOT_APPLICABLE)
        identity = self._resolve_identity(case)
        if identity is None:
            return CaseResult(case.case_id, DeletionStatus.INCOMPLETE)
        resources = self._investigator.investigate(
            identity, case.audit_at, reported_at=case.reported_at
        )
        assessment = assess(
            case,
            resources=resources,
            rules=self._tables.rules,
            authorized=True,
        )
        steps = build_steps(
            assessment,
            resources=resources,
            citations=visible_citations(self._tables, case.audit_at),
        )
        status = assessment.status
        if status is DeletionStatus.COMPLETE and not steps:
            # A completed deletion must be proven by cited evidence.
            logger.warning(
                "%s: COMPLETE has no evidence-backed step; INCOMPLETE", case.case_id
            )
            status = DeletionStatus.INCOMPLETE
        return CaseResult(case.case_id, status, tuple(steps))

    def _resolve_identity(self, case: Case):
        """Identity of the request, or ``None`` when the email stays ambiguous."""
        try:
            return self._resolver.resolve(case)
        except AmbiguousIdentityError as exc:
            logger.warning(
                "%s: %s; reporting INCOMPLETE without steps", case.case_id, exc
            )
            return None


def build_pipeline(
    tables: Tables, client: LLMClient | None = None
) -> tuple[AuditPipeline, LLMClient]:
    """Load all evidence once (images come from the response cache when present)."""
    client = client or LLMClient()
    skipped: list[str] = []
    records = extract_structured(tables) + extract_multimodal(
        tables, client, skipped=skipped
    )
    if skipped:
        logger.warning(
            "%d image(s) could not be read and were skipped: %s",
            len(skipped),
            ", ".join(skipped),
        )
    pipeline = AuditPipeline(
        tables, build_evidence_index(records), IntentClassifier(client)
    )
    return pipeline, client


def run_cases(
    pipeline: AuditPipeline, cases: Iterable[Case]
) -> tuple[list[CaseResult], dict[str, Exception]]:
    """Run every case; a failing case is reported, not allowed to stop the batch."""
    results: list[CaseResult] = []
    failures: dict[str, Exception] = {}
    for case in cases:
        try:
            results.append(pipeline.run_case(case))
        except Exception as exc:  # noqa: BLE001 - reported per case, run fails later
            logger.error("%s: %s: %s", case.case_id, type(exc).__name__, exc)
            failures[case.case_id] = exc
    return results, failures


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def case_sort_key(case_id: str) -> tuple[tuple[int, int | str], ...]:
    """Natural order (``case_9`` before ``case_10``), the order of the input files."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", case_id)
        if part
    )


def validate_results(
    expected_case_ids: Sequence[str], results: Sequence[CaseResult]
) -> None:
    """Check the output contract; raises ``OutputValidationError`` on any violation."""
    ids = [r.case_id for r in results]
    if len(set(ids)) != len(ids):
        raise OutputValidationError("duplicate case_id in results")
    if set(ids) != set(expected_case_ids) or len(ids) != len(expected_case_ids):
        missing = sorted(set(expected_case_ids) - set(ids), key=case_sort_key)
        extra = sorted(set(ids) - set(expected_case_ids), key=case_sort_key)
        raise OutputValidationError(
            f"case_id mismatch: {len(missing)} missing {missing[:5]}, "
            f"{len(extra)} unexpected {extra[:5]}"
        )
    if ids != sorted(ids, key=case_sort_key):
        raise OutputValidationError("rows are not sorted by case_id")
    for result in results:
        if result.status is DeletionStatus.COMPLETE and not result.steps:
            raise OutputValidationError(
                f"{result.case_id}: COMPLETE must have evidence-backed steps"
            )
        try:
            validate_plan(result.status, result.steps)
            # round trip through the serialized form that the grader parses
            decoded = json.loads(serialize_steps(result.steps))
            if [Step.from_dict(item) for item in decoded] != list(result.steps):
                raise InvalidStepError("steps do not survive JSON round trip")
        except InvalidStepError as exc:
            raise OutputValidationError(f"{result.case_id}: {exc}") from exc


def write_output(
    path: Path,
    expected_case_ids: Sequence[str],
    results: Iterable[CaseResult],
) -> list[CaseResult]:
    """Sort, validate and atomically write ``results`` to ``path`` (UTF-8, ``\\n``)."""
    ordered = sorted(results, key=lambda r: case_sort_key(r.case_id))
    validate_results(expected_case_ids, ordered)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=OUTPUT_COLUMNS, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(result.to_row() for result in ordered)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return ordered


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit deletion requests and write output.csv."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET_DIR,
        help="dataset directory (default: <repo>/dataset)",
    )
    parser.add_argument(
        "--cases",
        choices=("evaluation", "sample"),
        default="evaluation",
        help="which request file to audit (default: evaluation, 518 cases)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output CSV (default: <repo>/output.csv; required with --cases sample)",
    )
    args = parser.parse_args(argv)
    if args.output is None:
        if args.cases != "evaluation":
            parser.error("--output is required with --cases sample")
        args.output = DEFAULT_OUTPUT_PATH
    return args


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    args = parse_args(argv)
    tables = load_tables(args.dataset)
    cases = tables.cases if args.cases == "evaluation" else tables.sample_cases

    pipeline, client = build_pipeline(tables)
    results, failures = run_cases(pipeline, cases)
    if failures:
        logger.error(
            "%d of %d case(s) failed; output not written", len(failures), len(cases)
        )
        return 1

    try:
        ordered = write_output(args.output, [c.case_id for c in cases], results)
    except OutputValidationError as exc:
        logger.error("invalid output, nothing written: %s", exc)
        return 1

    counts: dict[str, int] = {}
    for result in ordered:
        counts[result.status.value] = counts.get(result.status.value, 0) + 1
    logger.info("wrote %d rows to %s: %s", len(ordered), args.output, counts)
    logger.info("LLM usage: %s", json.dumps(client.usage.summary(), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
