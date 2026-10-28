"""Markdown rendering for ``evaluation_report.md`` and ``usage_report.md``.

All numbers come from the run that is being reported; only the failure analysis
and the list of improvements are written by hand (``FAILURES`` and
``IMPROVEMENTS``) because they explain *why* numbers look the way they do.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from custom_cases import CustomOutcome
from instrumentation import PurposeUsage
from llm import ModelUsage
from metrics import CaseScore, StepKey, Summary

REGENERATE_COMMAND = "python3 code/evaluation/main.py"


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def fraction(numerator: int, denominator: int) -> str:
    return f"{numerator}/{denominator}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _key_text(key: StepKey) -> str:
    _, operation, system_id, resource_id = key
    return f"{operation} {system_id}/{resource_id}"


# ---------------------------------------------------------------------------
# Data handed over by the evaluation runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """LLM usage of the final full-dataset run (cache replays included)."""

    requests: int
    models: tuple[ModelUsage, ...]
    by_purpose: Mapping[str, PurposeUsage]
    intent_sources: Mapping[str, int]
    rule_fallback_cases: int
    rule_fallback_not_applicable: int
    prices_usd_per_mtok: Mapping[
        str, tuple[float, float, str]
    ]  # model -> in, out, origin


@dataclass(frozen=True, slots=True)
class FullRunSummary:
    """Sanity checks of the 518-case run."""

    cases: int
    status_counts: Mapping[str, int]
    contract_problems: tuple[str, ...]  # file-level and per-plan contract violations
    citation_problems: tuple[str, ...]
    empty_plan_cases: tuple[str, ...]
    matches_root_output: bool | None  # None when no root output.csv was compared
    root_output_path: str
    mismatching_cases: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Hand-written analysis
# ---------------------------------------------------------------------------

FAILURES: tuple[tuple[str, str], ...] = (
    (
        "Partial account coverage via the `reported_at` window",
        "`account_service` records carry no email, phone or hash, so they cannot "
        "be linked to a person by content. The investigator accepts the single "
        "account `DELETE_RECORD` activity within -90/+20 minutes of the case's "
        "`reported_at`. Accounts whose delete event falls outside that window, or "
        "where several deletes compete, are not found, which removes the "
        "`account_service` verification (and finding, when still present) from "
        "those answers (for example case_04).",
    ),
    (
        "Anonymized analytics linked by a second-of-minute heuristic",
        "Anonymized analytics profiles contain no identifier either. They are "
        "linked through the activity stream: the profile deletion appears "
        "40-42 seconds after the account delete. This matches every sample that "
        "has one, but it is a regularity of the data, not a documented rule, so "
        "an analytics record with a different offset would be missed.",
    ),
    (
        "Ids that appear in no visible source",
        "Some reference steps use resource ids (mostly security audit records "
        "and some accounts) that exist in no participant-visible file or "
        "activity. The planner never invents ids, so those steps are omitted. "
        "Each such case loses recall but never produces a wrong citation.",
    ),
    (
        "case_17 decoy (`account_6523`)",
        "A debug log of a profile lookup dated 2026-03-10 (`lookup_key` is the "
        "requester's email, result `rows=0 http_status=404`) names "
        "`account_6523`. The investigator links the resource through the email "
        "and adds an account verification that the reference does not contain: "
        "one surplus step in a single sample case. The lookup predates the "
        "request by months and found no row, so the reference most likely "
        "treats it as a decoy; this is an inference, since the reference gives "
        "no reason. Status and every other step of the case are correct.",
    ),
    (
        "Cited evidence file is not unique",
        "A resource is usually visible in several snapshots. The reference cites "
        "one specific file and its choice is not recoverable from the visible "
        "data; the planner cites the latest valid observation captured at or "
        "before `audit_at`, preferring a file shared by most resources of the "
        "case. Steps that match on resource and operation can therefore still "
        "differ in `evidence_id`; this is why strict step accuracy is lower than "
        "step accuracy.",
    ),
    (
        "Intent classification falls back to rules when the provider is unavailable",
        "A request text is classified by the model, and every accepted answer is "
        "cached. When the provider's daily token quota is exhausted, texts "
        "missing from the cache are classified by a multilingual keyword "
        "fallback instead (section 4 counts the texts decided by each source in "
        "the reported run). During development 31 texts, including the 10 "
        "non-deletion requests, were decided by the fallback for that reason. "
        "After the quota reset the model classified them, the answers were "
        "cached, and the 518 output rows did not change, so the fallback agreed "
        "with the model on those texts. The fallback remains a risk for any "
        "new, differently worded request: it can miss a negation or a language "
        "it has no keywords for.",
    ),
)

IMPROVEMENTS: tuple[str, ...] = (
    "**Time cutoff everywhere.** Evidence, activities and audit events later than "
    "`audit_at` are dropped at every stage, and the planner only cites ids that "
    "were visible at `audit_at` (checked on all 518 answers below).",
    "**Claims are not proof.** A `SUCCESS` activity is only accepted as a state "
    "when evidence shows the resulting state; `CHECK_*`, `SYNC_*` and `RESTORE_*` "
    "activities are used to detect active flows, restore paths and recreation "
    "after a deletion.",
    "**Account lookup anchored on `reported_at`.** Accounts have no identifiers, "
    "so the single account `DELETE_RECORD` activity near `reported_at` is used "
    "instead of guessing across the whole stream.",
    "**Support evidence classification.** Contact-index lookups map to "
    "`SECONDARY_IDENTIFIER` (delete) and business documents to "
    "`SUPPORT_ATTACHMENT` (delete) instead of `SUPPORT_MESSAGE` (redact); this "
    "fixed the policy treatment of case_07 and case_10.",
    "**Contract ordering.** Steps are ordered phase, operation table order, "
    "`system_id`, `resource_id`, with consecutive `step_no`, and natural `case_id` "
    "order (`case_2` before `case_10`); the writer validates everything before "
    "touching `output.csv`.",
    "**Fixed-step fallback.** The account and security-audit verifications that "
    "every deletion case has are emitted from whatever ids were found and are "
    "skipped, never invented, when none exist.",
    "**Reproducible LLM use.** Every accepted model answer is cached by a hash of "
    "provider, model, prompts and image bytes, so a second run makes no provider "
    "call; after the first provider failure the intent classifier stops calling "
    "the provider and uses the cache plus the keyword fallback.",
    "**Prompt-injection hardening.** Request text is sent only inside "
    "`<request>` tags with an instruction to treat it as data, and nothing else "
    "from the case is sent to the model (see custom case 4).",
)


# ---------------------------------------------------------------------------
# evaluation_report.md
# ---------------------------------------------------------------------------


def _summary_table(summary: Summary) -> str:
    rows = [
        (
            "Case-level deletion status accuracy",
            pct(summary.status_accuracy),
            fraction(summary.status_correct, summary.cases),
        ),
        (
            "Exact row accuracy (status and every step field)",
            pct(summary.row_accuracy),
            fraction(summary.exact_rows, summary.cases),
        ),
        (
            "Step precision (identity: phase, operation, system, resource)",
            pct(summary.step_precision),
            fraction(summary.matched_steps, summary.predicted_steps),
        ),
        (
            "Step recall (identity)",
            pct(summary.step_recall),
            fraction(summary.matched_steps, summary.expected_steps),
        ),
        ("Step F1 (identity)", pct(summary.step_f1), ""),
        (
            "Strict step precision (identity and `evidence_id`)",
            pct(summary.strict_step_precision),
            fraction(summary.strict_matched_steps, summary.predicted_steps),
        ),
        (
            "Strict step recall (identity and `evidence_id`)",
            pct(summary.strict_step_recall),
            fraction(summary.strict_matched_steps, summary.expected_steps),
        ),
        (
            "Sequence accuracy (identical ordered step list)",
            pct(summary.sequence_accuracy),
            fraction(summary.sequence_exact_cases, summary.cases),
        ),
        (
            "Sequence similarity (mean 2*LCS / (expected + predicted))",
            pct(summary.sequence_similarity_mean),
            "",
        ),
        (
            "Overall field accuracy (positional)",
            pct(summary.overall_field_accuracy),
            "",
        ),
    ]
    return _table(("Metric", "Value", "Count"), rows)


def _field_table(summary: Summary, names: Sequence[str]) -> str:
    rows = [
        (
            f"`{name}`",
            pct(summary.field_accuracy(name)),
            fraction(summary.field_hits[name], summary.field_positions),
        )
        for name in names
    ]
    return _table(("Field", "Accuracy", "Correct positions"), rows)


def _case_table(scores: Sequence[CaseScore]) -> str:
    rows = []
    for score in scores:
        rows.append(
            (
                score.case_id,
                f"{score.expected_status.value} / {score.predicted_status.value}",
                f"{len(score.expected_steps)} / {len(score.predicted_steps)}",
                score.matched_steps,
                len(score.missing_keys),
                len(score.extra_keys),
                score.matched_steps - score.strict_matched_steps,
                "yes" if score.sequence_exact else "no",
                "yes" if score.exact_row else "no",
            )
        )
    return _table(
        (
            "Case",
            "Status (reference / predicted)",
            "Steps (reference / predicted)",
            "Matched",
            "Missing",
            "Extra",
            "Other `evidence_id`",
            "Sequence exact",
            "Exact row",
        ),
        rows,
    )


def _difference_section(scores: Sequence[CaseScore]) -> str:
    differing = [s for s in scores if not s.exact_row]
    if not differing:
        return "Every sample row is identical to the reference."
    missing: Counter[str] = Counter()
    extra: Counter[str] = Counter()
    evidence_only = 0
    for score in differing:
        for key in score.missing_keys:
            missing[key[2]] += 1
        for key in score.extra_keys:
            extra[key[2]] += 1
        evidence_only += score.matched_steps - score.strict_matched_steps
    systems = sorted(set(missing) | set(extra))
    table = _table(
        ("system_id", "Missing steps", "Extra steps"),
        [(f"`{s}`", missing[s], extra[s]) for s in systems],
    )
    lines = [
        f"{len(differing)} of {len(scores)} sample rows differ from the reference. "
        f"{sum(missing.values())} reference steps are missing, "
        f"{sum(extra.values())} predicted steps are extra, and "
        f"{evidence_only} otherwise matching steps cite a different `evidence_id`.",
        "",
        table,
        "",
        "Differences per case (steps are `operation system_id/resource_id`):",
        "",
    ]
    for score in differing:
        parts: list[str] = []
        if not score.status_match:
            parts.append(
                f"status {score.predicted_status.value} "
                f"(reference {score.expected_status.value})"
            )
        if score.missing_keys:
            parts.append(
                "missing " + ", ".join(_key_text(k) for k in score.missing_keys)
            )
        if score.extra_keys:
            parts.append("extra " + ", ".join(_key_text(k) for k in score.extra_keys))
        other = score.matched_steps - score.strict_matched_steps
        if other:
            parts.append(f"{other} step(s) cite another evidence id")
        if not parts:
            parts.append("only `step_no` differs")
        lines.append(f"- {score.case_id}: " + "; ".join(parts))
    return "\n".join(lines)


def _custom_section(outcomes: Sequence[CustomOutcome]) -> str:
    rows = []
    for outcome in outcomes:
        custom = outcome.custom
        result = outcome.result
        rows.append(
            (
                custom.name,
                custom.description,
                custom.expectation,
                f"{result.status.value}, {len(result.steps)} step(s)",
                "PASS" if outcome.passed else "FAIL: " + "; ".join(outcome.failures),
            )
        )
    passed = sum(o.passed for o in outcomes)
    return "\n".join(
        [
            f"{passed} of {len(outcomes)} custom cases pass. Each case changes one "
            "thing in a real sample request, so the expectation does not need a "
            "hand-written golden answer. Every case must also give a plan that "
            "satisfies the step contract and cites nothing later than its "
            "`audit_at`.",
            "",
            _table(
                ("Case", "What it tests", "Expectation", "Observed", "Result"), rows
            ),
        ]
    )


def _full_run_section(full: FullRunSummary, usage: UsageSnapshot) -> str:
    counts = ", ".join(f"{k}: {v}" for k, v in sorted(full.status_counts.items()))
    lines = [
        f"The complete evaluation set ({full.cases} requests) was run end to end.",
        "",
        f"- Status distribution: {counts}.",
        "- Contract check (one row per `case_id`, sorted, valid `steps_json`, "
        "consecutive `step_no`, canonical order): "
        + (
            "passed."
            if not full.contract_problems
            else f"FAILED ({len(full.contract_problems)} problem(s)): "
            + "; ".join(full.contract_problems[:5])
        ),
        "- Citation check (every cited id exists and is not later than the case's "
        "`audit_at`): "
        + (
            "passed for all rows."
            if not full.citation_problems
            else f"FAILED ({len(full.citation_problems)}): "
            + "; ".join(full.citation_problems[:5])
        ),
    ]
    if full.matches_root_output is None:
        lines.append(
            f"- `{full.root_output_path}` was not found, so no comparison was made."
        )
    elif full.matches_root_output:
        lines.append(
            f"- The rows of this run are identical to `{full.root_output_path}` "
            "(the run is deterministic and the submitted file is current)."
        )
    else:
        lines.append(
            f"- The rows of this run differ from `{full.root_output_path}` for "
            f"{len(full.mismatching_cases)} case(s): "
            + ", ".join(full.mismatching_cases[:10])
            + ". Re-run `python3 code/main.py` to refresh it."
        )
    if full.empty_plan_cases:
        lines.append(
            f"- {len(full.empty_plan_cases)} deletion case(s) have an empty plan "
            "because the investigator found no resource in visible data "
            f"({', '.join(full.empty_plan_cases[:10])}"
            f"{', ...' if len(full.empty_plan_cases) > 10 else ''})."
        )
    llm_texts = usage.intent_sources.get("llm", 0)
    rule_texts = usage.intent_sources.get("rules", 0)
    lines.append(
        f"- Intent source: {llm_texts} request texts decided by the model "
        f"(cache or live), {rule_texts} by the keyword fallback "
        f"({usage.rule_fallback_not_applicable} of those are `NOT_APPLICABLE`)."
    )
    return "\n".join(lines)


def render_evaluation_report(
    *,
    sample_scores: Sequence[CaseScore],
    sample_summary: Summary,
    custom_outcomes: Sequence[CustomOutcome],
    full: FullRunSummary | None,
    usage: UsageSnapshot | None,
    field_names: Sequence[str],
) -> str:
    sections = [
        "# Evaluation report",
        "",
        f"Generated by `{REGENERATE_COMMAND}`. Everything in the tables below is "
        "computed from the run being reported; do not edit the numbers by hand.",
        "",
        "## 1. Method",
        "",
        "The pipeline is evaluated on the 32 public samples "
        "(`dataset/sample_deletion_requests.csv` against `dataset/sample_output.csv`), "
        "on custom cases built from those samples, and for sanity on the full "
        "518-request set (which has no reference answers).",
        "",
        "Metric definitions:",
        "",
        "- **Status accuracy:** predicted `deletion_status` equals the reference.",
        "- **Step accuracy:** a step is identified by `(phase, operation, "
        "system_id, resource_id)`; precision and recall are micro-averaged over "
        "all cases. *Strict* also requires the same `evidence_id`.",
        "- **Field accuracy:** step `i` of the prediction is compared with step "
        "`i` of the reference, field by field; missing or surplus positions "
        "count as wrong for every field, so one missing step also shifts the "
        "steps after it.",
        "- **Sequence accuracy:** the ordered list of step identities is "
        "identical to the reference. Sequence similarity is "
        "`2 * LCS / (expected + predicted)`, which only penalizes the steps "
        "that are out of order or absent.",
        "- **Exact row:** status and all steps (including `step_no` and "
        "`evidence_id`) equal the reference.",
        "",
        "Caveat: the pipeline was developed against these 32 samples, so the "
        "sample numbers are optimistic; there is no independent reference for "
        "the 518 evaluation requests.",
        "",
        "## 2. Results on the 32 public samples",
        "",
        _summary_table(sample_summary),
        "",
        "Field-level accuracy (positional):",
        "",
        _field_table(sample_summary, field_names),
        "",
        "### Per-case results",
        "",
        _case_table(sample_scores),
        "",
        "### Where the differences come from",
        "",
        _difference_section(sample_scores),
        "",
        "## 3. Custom evaluation cases",
        "",
        _custom_section(custom_outcomes),
        "",
    ]
    if full is not None and usage is not None:
        sections += [
            "## 4. Full evaluation set sanity checks",
            "",
            _full_run_section(full, usage),
            "",
        ]
    sections += [
        "## 5. Failures found",
        "",
    ]
    for index, (title, body) in enumerate(FAILURES, start=1):
        sections += [f"{index}. **{title}.** {body}", ""]
    sections += ["## 6. Improvements made", ""]
    sections += [f"- {item}" for item in IMPROVEMENTS]
    sections.append("")
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# usage_report.md
# ---------------------------------------------------------------------------


def _model_cost(model: ModelUsage, usage: UsageSnapshot) -> tuple[float, str]:
    prices = usage.prices_usd_per_mtok.get(model.model)
    if prices is None:
        return 0.0, "unknown"
    in_price, out_price, origin = prices
    tokens_in = model.input_tokens + model.cached_input_tokens
    tokens_out = model.output_tokens + model.cached_output_tokens
    return (tokens_in * in_price + tokens_out * out_price) / 1e6, origin


def render_usage_report(usage: UsageSnapshot) -> str:
    requests = usage.requests
    rows = []
    total_live = total_cached = total_in = total_out = 0
    total_cost = 0.0
    for model in usage.models:
        cost, _ = _model_cost(model, usage)
        tokens_in = model.input_tokens + model.cached_input_tokens
        tokens_out = model.output_tokens + model.cached_output_tokens
        total_live += model.calls
        total_cached += model.cached_calls
        total_in += tokens_in
        total_out += tokens_out
        total_cost += cost
        rows.append(
            (
                model.provider,
                f"`{model.model}`",
                model.calls,
                model.cached_calls,
                f"{tokens_in:,}",
                f"{tokens_out:,}",
                f"{tokens_in + tokens_out:,}",
                f"${cost:.4f}",
            )
        )
    rows.append(
        (
            "**Total**",
            "",
            total_live,
            total_cached,
            f"{total_in:,}",
            f"{total_out:,}",
            f"{total_in + total_out:,}",
            f"${total_cost:.4f}",
        )
    )
    per_request = [
        ("Model answers per request", f"{(total_live + total_cached) / requests:.2f}"),
        ("Input tokens per request", f"{total_in / requests:,.1f}"),
        ("Output tokens per request", f"{total_out / requests:,.1f}"),
        ("Total tokens per request", f"{(total_in + total_out) / requests:,.1f}"),
        ("Estimated cost per request", f"${total_cost / requests:.6f}"),
    ]
    purpose_rows = []
    for label, item in sorted(usage.by_purpose.items()):
        purpose_rows.append(
            (
                f"`{label}`",
                item.answers,
                item.live_answers,
                item.cached_answers,
                f"{item.input_tokens:,}",
                f"{item.output_tokens:,}",
                f"{item.input_tokens + item.output_tokens:,}",
            )
        )
    price_rows = [
        (f"`{model}`", f"${p_in:g}", f"${p_out:g}", origin)
        for model, (p_in, p_out, origin) in sorted(usage.prices_usd_per_mtok.items())
    ]
    lines = [
        "# Usage report",
        "",
        f"Generated by `{REGENERATE_COMMAND}`. It summarizes the final full-dataset "
        f"run of `python3 code/main.py` over all {requests} requests of "
        "`dataset/deletion_requests.csv`, the run that produced the submitted "
        "`output.csv`. No API key or credential appears in this file.",
        "",
        "## How to read the numbers",
        "",
        "- *Live calls* are provider calls made during the reported run. "
        "*Cached answers* were served from the on-disk response cache "
        "(`.llm_cache/`) and cost no provider call in this run; their tokens "
        "are the tokens the same answers cost when they were first fetched. "
        "Token and cost totals below include both, so they describe what the "
        "pipeline's answers cost in total, not only what this run spent.",
        "- The LLM is used in two places only: classifying the request text "
        "(`request_intent`, one short call per distinct text) and reading the "
        "application screenshots in the evidence (`image_extraction`, one call "
        "per image, loaded once for all requests). Everything else is "
        "deterministic code.",
        "- Request texts decided by the keyword fallback make no model call "
        f"({usage.intent_sources.get('rules', 0)} of {requests} in this run).",
        "",
        "## Totals per model and overall",
        "",
        _table(
            (
                "Provider",
                "Model",
                "Live calls",
                "Cached answers",
                "Input tokens",
                "Output tokens",
                "Total tokens",
                "Estimated cost",
            ),
            rows,
        ),
        "",
        "## Per request",
        "",
        f"Averages over {requests} requests.",
        "",
        _table(("Quantity", "Value"), per_request),
        "",
        "## By purpose",
        "",
        _table(
            (
                "Purpose",
                "Answers",
                "Live",
                "Cached",
                "Input tokens",
                "Output tokens",
                "Total tokens",
            ),
            purpose_rows,
        ),
        "",
        "## Pricing assumptions",
        "",
        "Prices are USD per million tokens. They are estimates, not billing data.",
        "",
        _table(("Model", "Input", "Output", "Origin"), price_rows),
        "",
        "Set `LLM_PRICE_INPUT_PER_MTOK` and `LLM_PRICE_OUTPUT_PER_MTOK` in the "
        "environment and re-run `python3 code/evaluation/main.py` to recompute "
        "the cost with the real prices.",
        "",
    ]
    return "\n".join(lines)
