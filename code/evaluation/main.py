"""Evaluate the audit pipeline and write the two required reports.

Usage (from the repository root)::

    python3 code/evaluation/main.py                 # full evaluation, both reports
    python3 code/evaluation/main.py --skip-full     # samples and custom cases only

What it does:

1. loads the dataset and builds the same pipeline as ``code/main.py``, wrapped
   so that LLM usage and intent decisions are recorded;
2. runs all evaluation requests and checks the answers against the output
   contract and the ``audit_at`` citation rule, and compares them with the
   root ``output.csv`` (skipped with ``--skip-full``);
3. runs the 32 public samples and scores them against ``sample_output.csv``;
4. runs the custom cases of ``custom_cases.py``;
5. writes ``evaluation_report.md`` and ``usage_report.md`` next to this file.

The full run happens first so that the usage snapshot covers exactly the
evaluation requests. With a warm response cache it makes no provider call.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

# ``code/main.py`` must win over this file's own name when importing ``main``.
_EVALUATION_DIR = Path(__file__).resolve().parent
_CODE_DIR = _EVALUATION_DIR.parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _EVALUATION_DIR]
sys.path.insert(0, str(_CODE_DIR))
sys.path.append(str(_EVALUATION_DIR))

from custom_cases import (
    CustomOutcome,
    build_custom_cases,
    run_custom_cases,
)  # noqa: E402
from instrumentation import CountingClassifier, MeteredClient  # noqa: E402
from invariants import CitationIndex  # noqa: E402
from llm import PRICE_TABLE, LLMConfig  # noqa: E402
from main import (  # noqa: E402
    OUTPUT_COLUMNS,
    AuditPipeline,
    CaseResult,
    OutputValidationError,
    case_sort_key,
    run_cases,
    validate_results,
)
from metrics import CaseScore, summarize  # noqa: E402
from models import STEP_FIELDS, Case, DeletionStatus, Step  # noqa: E402
from parser import (  # noqa: E402
    DEFAULT_DATASET_DIR,
    REPO_ROOT,
    Tables,
    build_evidence_index,
    extract_multimodal,
    extract_structured,
    load_tables,
)
from reports import (  # noqa: E402
    FullRunSummary,
    UsageSnapshot,
    render_evaluation_report,
    render_usage_report,
)

logger = logging.getLogger("evaluation")

DEFAULT_ROOT_OUTPUT = REPO_ROOT / "output.csv"

# Used only when neither the environment nor the built-in table has a price.
# USD per million (input, output) tokens: the list price of a comparable model
# (Qwen3-32B on Groq), because no price is published for the configured id.
ASSUMED_PRICE: tuple[float, float] = (0.29, 0.59)
ASSUMED_PRICE_NOTE = (
    "assumed: list price of Qwen3-32B on Groq used as a proxy "
    "(no price known for this model id)"
)


# ---------------------------------------------------------------------------
# Pipeline construction
# ---------------------------------------------------------------------------


def build_instrumented_pipeline(
    tables: Tables,
) -> tuple[AuditPipeline, MeteredClient, CountingClassifier]:
    """Same stages as ``main.build_pipeline`` with usage instrumentation."""
    client = MeteredClient()
    skipped: list[str] = []
    records = extract_structured(tables) + extract_multimodal(
        tables, client, skipped=skipped
    )
    if skipped:
        logger.warning("%d image(s) could not be read: %s", len(skipped), skipped)
    classifier = CountingClassifier(client)
    pipeline = AuditPipeline(tables, build_evidence_index(records), classifier)
    return pipeline, client, classifier


# ---------------------------------------------------------------------------
# Output file handling
# ---------------------------------------------------------------------------


def read_output_csv(path: Path) -> list[CaseResult]:
    """Parse an ``output.csv`` into results; raises ``OutputValidationError``."""
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != OUTPUT_COLUMNS:
            raise OutputValidationError(
                f"header must be {','.join(OUTPUT_COLUMNS)}, got {reader.fieldnames}"
            )
        results: list[CaseResult] = []
        for row in reader:
            try:
                steps = tuple(Step.from_dict(s) for s in json.loads(row["steps_json"]))
                status = DeletionStatus(row["deletion_status"])
            except (ValueError, TypeError) as exc:
                raise OutputValidationError(f"{row.get('case_id')}: {exc}") from exc
            results.append(CaseResult(row["case_id"], status, steps))
    return results


def compare_with_root_output(
    path: Path, expected_ids: Sequence[str], results: Sequence[CaseResult]
) -> tuple[bool | None, list[str], list[str]]:
    """``(matches, contract_problems, differing_case_ids)`` for ``path``."""
    if not path.is_file():
        return None, [], []
    try:
        submitted = read_output_csv(path)
        validate_results(expected_ids, submitted)
    except OutputValidationError as exc:
        return False, [f"{path.name}: {exc}"], []
    by_id = {r.case_id: r for r in results}
    differing = [r.case_id for r in submitted if by_id.get(r.case_id) != r]
    return not differing, [], differing


# ---------------------------------------------------------------------------
# Full-run summary and usage snapshot
# ---------------------------------------------------------------------------


def summarize_full_run(
    tables: Tables,
    results: Sequence[CaseResult],
    root_output: Path,
) -> FullRunSummary:
    ordered = sorted(results, key=lambda r: case_sort_key(r.case_id))
    expected_ids = [c.case_id for c in tables.cases]
    problems: list[str] = []
    try:
        validate_results(expected_ids, ordered)
    except OutputValidationError as exc:
        problems.append(str(exc))

    citations = CitationIndex(tables)
    cases = {c.case_id: c for c in tables.cases}
    citation_problems = [
        f"{r.case_id}: {p}"
        for r in ordered
        for p in citations.violations(r.steps, cases[r.case_id].audit_at)
    ]
    status_counts: dict[str, int] = {}
    for result in ordered:
        status_counts[result.status.value] = (
            status_counts.get(result.status.value, 0) + 1
        )
    empty = tuple(
        r.case_id
        for r in ordered
        if r.status is not DeletionStatus.NOT_APPLICABLE and not r.steps
    )
    matches, file_problems, differing = compare_with_root_output(
        root_output, expected_ids, ordered
    )
    try:
        shown = str(root_output.relative_to(REPO_ROOT))
    except ValueError:
        shown = str(root_output)
    return FullRunSummary(
        cases=len(ordered),
        status_counts=status_counts,
        contract_problems=tuple(problems + file_problems),
        citation_problems=tuple(citation_problems),
        empty_plan_cases=empty,
        matches_root_output=matches,
        root_output_path=shown,
        mismatching_cases=tuple(differing),
    )


def resolve_prices(
    config: LLMConfig, models: Sequence[str]
) -> dict[str, tuple[float, float, str]]:
    prices: dict[str, tuple[float, float, str]] = {}
    for model in models:
        if model == config.model and (
            config.price_input_per_mtok is not None
            and config.price_output_per_mtok is not None
        ):
            prices[model] = (
                config.price_input_per_mtok,
                config.price_output_per_mtok,
                "environment (LLM_PRICE_INPUT_PER_MTOK / LLM_PRICE_OUTPUT_PER_MTOK)",
            )
        elif model in PRICE_TABLE:
            prices[model] = (*PRICE_TABLE[model], "built-in table in `code/llm.py`")
        else:
            prices[model] = (*ASSUMED_PRICE, ASSUMED_PRICE_NOTE)
    return prices


def snapshot_usage(
    tables: Tables,
    client: MeteredClient,
    classifier: CountingClassifier,
    results: Sequence[CaseResult],
) -> UsageSnapshot:
    models = tuple(client.usage.models())
    fallback_results = [
        r
        for r, case in zip(results, tables.cases)
        if case.request_text in classifier.rule_texts
    ]
    return UsageSnapshot(
        requests=len(tables.cases),
        models=models,
        by_purpose=client.purpose_snapshot(),
        intent_sources=dict(classifier.sources),
        rule_fallback_cases=len(fallback_results),
        rule_fallback_not_applicable=sum(
            r.status is DeletionStatus.NOT_APPLICABLE for r in fallback_results
        ),
        prices_usd_per_mtok=resolve_prices(client.config, [m.model for m in models]),
    )


# ---------------------------------------------------------------------------
# Sample scoring
# ---------------------------------------------------------------------------


def score_samples(tables: Tables, results: Sequence[CaseResult]) -> list[CaseScore]:
    predicted = {r.case_id: r for r in results}
    scores = []
    for reference in sorted(
        tables.sample_outputs, key=lambda r: case_sort_key(r.case_id)
    ):
        result = predicted[reference.case_id]
        scores.append(
            CaseScore(
                reference.case_id,
                reference.deletion_status,
                result.status,
                reference.steps,
                result.steps,
            )
        )
    return scores


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the pipeline and write the evaluation and usage reports."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_DIR)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_ROOT_OUTPUT,
        help="submitted output.csv to cross-check (default: <repo>/output.csv)",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=_EVALUATION_DIR,
        help="where to write the reports (default: this directory)",
    )
    parser.add_argument(
        "--skip-full",
        action="store_true",
        help="skip the 518-request run; the usage report is then not written",
    )
    return parser.parse_args(argv)


def _assert_no_secrets(text: str, env: Mapping[str, str]) -> None:
    """Refuse to write a report that contains the value of a credential."""
    for name, value in env.items():
        upper = name.upper()
        if len(value) >= 8 and any(w in upper for w in ("KEY", "TOKEN", "SECRET")):
            if value in text:
                raise RuntimeError(f"report would leak the value of {name}")


def write_report(path: Path, text: str) -> None:
    _assert_no_secrets(text, os.environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    logger.info("wrote %s", path)


def run_or_fail(
    pipeline: AuditPipeline, cases: Sequence[Case], label: str
) -> list[CaseResult]:
    results, failures = run_cases(pipeline, cases)
    if failures:
        raise SystemExit(f"{label}: {len(failures)} of {len(cases)} case(s) failed")
    return results


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    args = parse_args(argv)
    tables = load_tables(args.dataset)
    pipeline, client, classifier = build_instrumented_pipeline(tables)

    full: FullRunSummary | None = None
    usage: UsageSnapshot | None = None
    if not args.skip_full:
        full_results = run_or_fail(pipeline, tables.cases, "full run")
        full = summarize_full_run(tables, full_results, args.output)
        usage = snapshot_usage(tables, client, classifier, full_results)

    sample_results = run_or_fail(pipeline, tables.sample_cases, "sample run")
    scores = score_samples(tables, sample_results)
    customs = build_custom_cases(tables, pipeline)
    outcomes: list[CustomOutcome] = run_custom_cases(pipeline, customs)

    summary = summarize(scores)
    write_report(
        args.reports_dir / "evaluation_report.md",
        render_evaluation_report(
            sample_scores=scores,
            sample_summary=summary,
            custom_outcomes=outcomes,
            full=full,
            usage=usage,
            field_names=STEP_FIELDS,
        ),
    )
    if usage is not None:
        write_report(args.reports_dir / "usage_report.md", render_usage_report(usage))
    else:
        logger.info("usage_report.md not written (--skip-full)")

    logger.info(
        "samples: status %d/%d, exact rows %d/%d; custom cases passed %d/%d",
        summary.status_correct,
        summary.cases,
        summary.exact_rows,
        summary.cases,
        sum(o.passed for o in outcomes),
        len(outcomes),
    )
    failed = bool(
        full and (full.contract_problems or full.citation_problems)
    ) or not all(o.passed for o in outcomes)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
