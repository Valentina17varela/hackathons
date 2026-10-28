"""CSV loading and time utilities for the deletion-audit pipeline.

``load_tables()`` reads every participant-facing CSV under ``dataset/`` into
typed, immutable records without dropping or rewriting any row. Timestamps are
parsed as timezone-aware UTC datetimes, and ``at_or_before`` implements the
rule that only information available at or before ``audit_at`` may be used.

Evidence *contents* are extracted by ``extract_structured()`` (SQL, JSON, TXT
and logs; implemented in ``evidence_extractors``) and by ``extract_multimodal()``
(PDFs via a local extractor, images via cached LLM vision; implemented in
``multimodal_extractors`` and ``llm``); the rest of this module loads the
tabular files and the evidence manifest.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, Sequence, TypeVar

if TYPE_CHECKING:
    from llm import LLMClient

from models import (
    Case,
    DeletionRule,
    DeletionStatus,
    EvidenceRecord,
    ReportedStatus,
    RuleAction,
    Step,
)

T = TypeVar("T")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_DIR = REPO_ROOT / "dataset"

# ---------------------------------------------------------------------------
# Time utilities
# ---------------------------------------------------------------------------


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC ``datetime``.

    Accepts a trailing ``Z`` or an explicit UTC offset. Naive timestamps are
    rejected because the dataset contract defines every time as UTC.
    """
    text = value.strip()
    if not text:
        raise ValueError("empty timestamp")
    if text[-1] in "zZ":
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp has no UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc)


def format_utc(moment: datetime) -> str:
    """Format a datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (the dataset's format)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime cannot be formatted as UTC")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def at_or_before(
    items: Iterable[T],
    audit_at: datetime,
    key: Callable[[T], datetime],
) -> list[T]:
    """Return the items whose ``key(item)`` is at or before ``audit_at``.

    The comparison is inclusive: an item timestamped exactly at ``audit_at``
    is considered available. Input order is preserved.
    """
    if audit_at.tzinfo is None:
        raise ValueError("audit_at must be timezone-aware")
    return [item for item in items if key(item) <= audit_at]


# ---------------------------------------------------------------------------
# Row records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserIdentity:
    """Current identity snapshot of one user (``user_identities.csv``)."""

    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Historical change to an identity field (``user_audit_log.csv``)."""

    audit_id: str
    user_id: str
    event_time: datetime
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True, slots=True)
class Activity:
    """One row of the global activity stream (``system_activity.csv``).

    ``reported_result`` is a claim, not proof of the resulting state.
    """

    activity_id: str
    event_time: datetime
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    """Manifest entry for one evidence file (``evidence_files.csv``)."""

    file_id: str
    file_path: str
    origin: str
    captured_at: datetime

    def resolve(self, dataset_dir: Path = DEFAULT_DATASET_DIR) -> Path:
        """Absolute path of the evidence file under ``dataset_dir``."""
        return dataset_dir / self.file_path


@dataclass(frozen=True, slots=True)
class SampleOutput:
    """Expected output row for a public sample case (``sample_output.csv``)."""

    case_id: str
    deletion_status: DeletionStatus
    steps: tuple[Step, ...]


# ---------------------------------------------------------------------------
# Tables container
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tables:
    """All tabular participant-facing data, in file order."""

    cases: tuple[Case, ...]
    sample_cases: tuple[Case, ...]
    sample_outputs: tuple[SampleOutput, ...]
    identities: tuple[UserIdentity, ...]
    audit_events: tuple[AuditEvent, ...]
    activities: tuple[Activity, ...]
    rules: tuple[DeletionRule, ...]
    evidence_files: tuple[EvidenceFile, ...]
    dataset_dir: Path = DEFAULT_DATASET_DIR

    def case_by_id(self, case_id: str) -> Case:
        return {c.case_id: c for c in self.cases}[case_id]

    def evidence_by_id(self, file_id: str) -> EvidenceFile:
        return {e.file_id: e for e in self.evidence_files}[file_id]

    def identity_by_user_id(self, user_id: str) -> UserIdentity:
        return {i.user_id: i for i in self.identities}[user_id]

    def activities_until(self, audit_at: datetime) -> list[Activity]:
        return at_or_before(self.activities, audit_at, lambda a: a.event_time)

    def audit_events_until(self, audit_at: datetime) -> list[AuditEvent]:
        return at_or_before(self.audit_events, audit_at, lambda e: e.event_time)

    def evidence_files_until(self, audit_at: datetime) -> list[EvidenceFile]:
        return at_or_before(self.evidence_files, audit_at, lambda e: e.captured_at)


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------

CASE_COLUMNS = (
    "case_id",
    "email",
    "requested_at",
    "reported_status",
    "reported_at",
    "audit_at",
    "request_text",
)
IDENTITY_COLUMNS = ("user_id", "name", "email", "phone", "email_hash", "updated_at")
AUDIT_COLUMNS = (
    "audit_id",
    "user_id",
    "event_time",
    "field_name",
    "old_value",
    "new_value",
)
ACTIVITY_COLUMNS = (
    "activity_id",
    "event_time",
    "event_type",
    "source_system",
    "target_system",
    "resource_id",
    "reported_result",
    "details",
)
RULE_COLUMNS = ("rule_id", "data_category", "condition", "required_action")
EVIDENCE_COLUMNS = ("file_id", "file_path", "origin", "captured_at")
SAMPLE_OUTPUT_COLUMNS = ("case_id", "deletion_status", "steps_json")


class DatasetError(ValueError):
    """Raised when a dataset file is malformed or violates its contract."""


def _read_rows(path: Path, columns: Sequence[str]) -> Iterator[dict[str, str]]:
    """Yield rows as dicts after checking the exact header."""
    if not path.is_file():
        raise DatasetError(f"missing dataset file: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != tuple(columns):
            raise DatasetError(
                f"{path.name}: expected columns {list(columns)}, got {reader.fieldnames}"
            )
        for line_no, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise DatasetError(f"{path.name}:{line_no}: wrong number of fields")
            yield row


def _load(
    path: Path, columns: Sequence[str], build: Callable[[dict[str, str]], T]
) -> tuple[T, ...]:
    rows: list[T] = []
    for index, row in enumerate(_read_rows(path, columns), start=2):
        try:
            rows.append(build(row))
        except (ValueError, KeyError) as exc:
            raise DatasetError(f"{path.name}:{index}: {exc}") from exc
    return tuple(rows)


def _ensure_unique(name: str, rows: Iterable[T], key: Callable[[T], str]) -> None:
    seen: set[str] = set()
    for row in rows:
        value = key(row)
        if value in seen:
            raise DatasetError(f"{name}: duplicate id {value!r}")
        seen.add(value)


def _build_case(row: dict[str, str]) -> Case:
    return Case(
        case_id=row["case_id"],
        email=row["email"],
        requested_at=parse_utc(row["requested_at"]),
        reported_status=ReportedStatus(row["reported_status"]),
        reported_at=parse_utc(row["reported_at"]),
        audit_at=parse_utc(row["audit_at"]),
        request_text=row["request_text"],
    )


def _build_identity(row: dict[str, str]) -> UserIdentity:
    return UserIdentity(
        user_id=row["user_id"],
        name=row["name"],
        email=row["email"],
        phone=row["phone"],
        email_hash=row["email_hash"],
        updated_at=parse_utc(row["updated_at"]),
    )


def _build_audit_event(row: dict[str, str]) -> AuditEvent:
    return AuditEvent(
        audit_id=row["audit_id"],
        user_id=row["user_id"],
        event_time=parse_utc(row["event_time"]),
        field_name=row["field_name"],
        old_value=row["old_value"],
        new_value=row["new_value"],
    )


def _build_activity(row: dict[str, str]) -> Activity:
    return Activity(
        activity_id=row["activity_id"],
        event_time=parse_utc(row["event_time"]),
        event_type=row["event_type"],
        source_system=row["source_system"],
        target_system=row["target_system"],
        resource_id=row["resource_id"],
        reported_result=row["reported_result"],
        details=row["details"],
    )


def _build_rule(row: dict[str, str]) -> DeletionRule:
    return DeletionRule(
        rule_id=row["rule_id"],
        data_category=row["data_category"],
        condition=row["condition"],
        required_action=RuleAction(row["required_action"]),
    )


def _build_evidence_file(row: dict[str, str]) -> EvidenceFile:
    return EvidenceFile(
        file_id=row["file_id"],
        file_path=row["file_path"],
        origin=row["origin"],
        captured_at=parse_utc(row["captured_at"]),
    )


def _build_sample_output(row: dict[str, str]) -> SampleOutput:
    raw_steps = json.loads(row["steps_json"])
    if not isinstance(raw_steps, list):
        raise ValueError("steps_json must be a JSON array")
    return SampleOutput(
        case_id=row["case_id"],
        deletion_status=DeletionStatus(row["deletion_status"]),
        steps=tuple(Step.from_dict(item) for item in raw_steps),
    )


def load_tables(dataset_dir: Path | str = DEFAULT_DATASET_DIR) -> Tables:
    """Load every tabular participant-facing file from ``dataset_dir``.

    Row order from each file is preserved and no row is dropped. Duplicate
    primary identifiers or malformed rows raise ``DatasetError``.
    """
    root = Path(dataset_dir)

    cases = _load(root / "deletion_requests.csv", CASE_COLUMNS, _build_case)
    sample_cases = _load(
        root / "sample_deletion_requests.csv", CASE_COLUMNS, _build_case
    )
    sample_outputs = _load(
        root / "sample_output.csv", SAMPLE_OUTPUT_COLUMNS, _build_sample_output
    )
    identities = _load(root / "user_identities.csv", IDENTITY_COLUMNS, _build_identity)
    audit_events = _load(root / "user_audit_log.csv", AUDIT_COLUMNS, _build_audit_event)
    activities = _load(root / "system_activity.csv", ACTIVITY_COLUMNS, _build_activity)
    rules = _load(root / "deletion_rules.csv", RULE_COLUMNS, _build_rule)
    evidence_files = _load(
        root / "evidence_files.csv", EVIDENCE_COLUMNS, _build_evidence_file
    )

    _ensure_unique("deletion_requests", cases, lambda r: r.case_id)
    _ensure_unique("sample_deletion_requests", sample_cases, lambda r: r.case_id)
    _ensure_unique("sample_output", sample_outputs, lambda r: r.case_id)
    _ensure_unique("user_identities", identities, lambda r: r.user_id)
    _ensure_unique("user_audit_log", audit_events, lambda r: r.audit_id)
    _ensure_unique("system_activity", activities, lambda r: r.activity_id)
    _ensure_unique("deletion_rules", rules, lambda r: r.rule_id)
    _ensure_unique("evidence_files", evidence_files, lambda r: r.file_id)

    return Tables(
        cases=cases,
        sample_cases=sample_cases,
        sample_outputs=sample_outputs,
        identities=identities,
        audit_events=audit_events,
        activities=activities,
        rules=rules,
        evidence_files=evidence_files,
        dataset_dir=root,
    )


# ---------------------------------------------------------------------------
# Structured evidence extraction
# ---------------------------------------------------------------------------
# ``evidence_extractors`` imports this module, so it is imported lazily here.


def extract_structured(tables: Tables) -> list[EvidenceRecord]:
    """Extract ``EvidenceRecord`` objects from every non-multimodal evidence file.

    SQL snapshots run in a per-file in-memory SQLite (primary and replica tables
    are compared), JSON/TXT/log files are parsed into records, and PDFs/images
    are skipped (see the multimodal extractor). Order follows the manifest.
    """
    from evidence_extractors import extract_structured_files

    return extract_structured_files(tables.evidence_files, tables.dataset_dir)


def extract_multimodal(
    tables: Tables,
    client: LLMClient | None = None,
    *,
    strict: bool = False,
    skipped: list[str] | None = None,
) -> list[EvidenceRecord]:
    """Extract ``EvidenceRecord`` objects from every PDF and screenshot.

    PDFs are read with a local text extractor and parsed deterministically;
    only images use LLM vision. Every image answer is cached on disk, so a
    second run makes no provider calls (and needs no API key). An image with
    no cached answer and no API key raises ``LLMUnavailableError`` when
    ``strict`` is true; otherwise it is skipped with a warning and its
    ``file_id`` is appended to ``skipped``. Order follows the manifest.
    """
    from multimodal_extractors import (
        extract_multimodal_files,
        reconcile_screenshot_emails,
    )

    records = extract_multimodal_files(
        tables.evidence_files,
        tables.dataset_dir,
        client,
        strict=strict,
        skipped=skipped,
    )
    return reconcile_screenshot_emails(records, tables.identities, tables.audit_events)


def build_evidence_index(records: Iterable[EvidenceRecord]):
    """Index records by email, phone, email hash, user id and resource id."""
    from evidence_extractors import EvidenceIndex

    return EvidenceIndex(records)
