"""Structured evidence extractors (SQL, JSON, TXT and log files).

Every non-multimodal evidence file listed in ``evidence_files.csv`` is turned
into ``EvidenceRecord`` objects:

* ``.sql``  -- executed in a per-file in-memory SQLite database; every row of
  every table becomes one record and primary/replica tables of the same file
  are compared to flag replication drift.
* ``.json`` -- walked recursively; every object that carries a ``resource_id``
  becomes one record (nested child records are emitted separately).
* ``.txt``  -- pipe-delimited handover lines.
* ``.log``  -- every supported line syntax is parsed into key/value fields and
  the lines of one trace are merged into a single record.

Each record carries normalized identifier lists (``emails``, ``phones``,
``email_hashes``, ``user_ids``, ``names``) so ``EvidenceIndex`` can look
records up by email, phone, email hash, user id or resource id regardless of
how the source system named the field.

``EvidenceRecord.captured_at`` is the manifest capture time of the file (the
time that governs whether the file may be cited for a case); the time of the
individual observation is stored in ``fields["observed_at"]``.

PDFs and images are handled by the multimodal extractor, not here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from models import EvidenceRecord, Identity
from parser import (
    DEFAULT_DATASET_DIR,
    DatasetError,
    EvidenceFile,
    format_utc,
    parse_utc,
)

STRUCTURED_SUFFIXES = frozenset({".sql", ".json", ".txt", ".log"})
MULTIMODAL_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"})

# Resource-id prefix -> owning system, as used by the reported steps.
RESOURCE_SYSTEM_PREFIXES: tuple[tuple[str, str], ...] = (
    ("account_", "account_service"),
    ("analytics_", "analytics_platform"),
    ("audit_record_", "security_service"),
    ("backup_", "backup_service"),
    ("cache_", "cache_service"),
    ("data_flow_", "integration_service"),
    ("email_delivery_", "email_delivery_service"),
    ("export_", "export_service"),
    ("legal_hold_", "legal_service"),
    ("marketing_", "marketing_platform"),
    ("object_", "object_storage"),
    ("search_", "search_service"),
    ("support_", "support_platform"),
    ("tombstone_", "identity_service"),
)

# ---------------------------------------------------------------------------
# Identifier normalization and extraction
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"
)
_USER_ID_RE = re.compile(r"(?<![A-Za-z0-9_])user_\d+(?![A-Za-z0-9_])")
_HASH_RE = re.compile(r"(?<![0-9A-Za-z:])[0-9a-fA-F]{64}(?![0-9A-Za-z])")
_PHONE_VALUE_RE = re.compile(r"^\+?\d[\d\s().\-]{5,}\d$")
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

_NAME_KEYS = frozenset(
    {
        "name",
        "full_name",
        "display_name",
        "requester_name",
        "customer_name",
        "owner_name",
    }
)
# Keys whose value may be any kind of identifier (so a bare phone number counts).
_LOOKUP_KEYS = frozenset(
    {
        "lookup_key",
        "lookup_value",
        "match_key",
        "subject_key",
        "owner_key",
        "correlation_key",
        "subject_reference",
        "subject_selector",
        "subject_ref",
        "matched_value",
        "query",
    }
)
_OPAQUE_KEY_PARTS = ("seal", "checksum", "signature", "fingerprint")

IDENTIFIER_FIELDS = ("emails", "phones", "email_hashes", "user_ids", "names")


def normalize_email(value: str) -> str:
    """Case-fold an email address for matching."""
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    """Reduce a phone number to its digits; ``""`` when it is too short."""
    digits = re.sub(r"\D", "", value)
    return digits if len(digits) >= 7 else ""


def normalize_hash(value: str) -> str:
    """Case-fold a hex digest for matching."""
    return value.strip().lower()


def _iter_strings(value: Any, key: str) -> Iterator[tuple[str, str]]:
    """Yield ``(leaf key, string)`` for every string nested inside ``value``."""
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, Mapping):
        for child_key, child in value.items():
            yield from _iter_strings(child, str(child_key))
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_strings(child, key)


def collect_identifiers(fields: Mapping[str, Any]) -> dict[str, list[str]]:
    """Find normalized identifiers in ``fields``, regardless of field naming.

    Emails, user ids and email hashes are located by shape inside any string
    (including free text); phones are taken from ``*phone*`` fields, from
    ``+``-prefixed values and from lookup-style fields; names come from known
    name fields. Every list is sorted and de-duplicated.
    """
    emails: set[str] = set()
    phones: set[str] = set()
    hashes: set[str] = set()
    user_ids: set[str] = set()
    names: set[str] = set()
    for key, text in _iter_strings(fields, ""):
        leaf = key.rsplit(".", 1)[-1].lower()
        if any(part in leaf for part in _OPAQUE_KEY_PARTS):
            continue
        stripped = text.strip()
        if not stripped:
            continue
        emails.update(normalize_email(m) for m in _EMAIL_RE.findall(stripped))
        user_ids.update(_USER_ID_RE.findall(stripped))
        hashes.update(normalize_hash(m) for m in _HASH_RE.findall(stripped))
        if leaf in _NAME_KEYS:
            names.add(stripped)
        is_phone_key = "phone" in leaf
        looks_like_phone = bool(_PHONE_VALUE_RE.match(stripped)) and (
            stripped.startswith("+")
            or (leaf in _LOOKUP_KEYS and not _DATE_PREFIX_RE.match(stripped))
        )
        if (is_phone_key and "@" not in stripped) or looks_like_phone:
            phone = normalize_phone(stripped)
            if phone:
                phones.add(phone)
    return {
        "emails": sorted(emails),
        "phones": sorted(phones),
        "email_hashes": sorted(hashes),
        "user_ids": sorted(user_ids),
        "names": sorted(names),
    }


def system_for_resource(resource_id: str, declared: str | None = None) -> str:
    """Owning system of ``resource_id`` (prefix first, then the declared system)."""
    for prefix, system in RESOURCE_SYSTEM_PREFIXES:
        if resource_id.startswith(prefix):
            return system
    return declared or ""


# ---------------------------------------------------------------------------
# Record construction helpers
# ---------------------------------------------------------------------------

_OBSERVED_KEYS = ("observed_at", "recorded_at", "updated_at")


def _to_utc_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return format_utc(parse_utc(value))
    except ValueError:
        return None


def _make_record(
    ef: EvidenceFile,
    *,
    resource_id: str,
    raw_fields: Mapping[str, Any],
    raw_text: str,
    declared_system: str | None,
    fallback_observed_at: str | None,
    extra: Mapping[str, Any] | None = None,
) -> EvidenceRecord:
    """Assemble a record with normalized identifiers and observation time."""
    fields: dict[str, Any] = dict(raw_fields)
    observed_at = None
    for key in _OBSERVED_KEYS:
        observed_at = _to_utc_text(fields.get(key))
        if observed_at:
            break
    observed_at = observed_at or fallback_observed_at or format_utc(ef.captured_at)
    system_id = system_for_resource(resource_id, declared_system)
    if declared_system and declared_system != system_id:
        fields["reported_system"] = declared_system
    fields.update(collect_identifiers(fields))
    fields["observed_at"] = observed_at
    if extra:
        fields.update(extra)
    return EvidenceRecord(
        evidence_id=ef.file_id,
        captured_at=ef.captured_at,
        system_id=system_id or ef.origin,
        resource_id=resource_id,
        fields=fields,
        raw_text=raw_text,
        source_path=ef.file_path,
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# JSON extractor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Context:
    system: str | None = None
    collection: str | None = None
    observed_at: str | None = None


def _derive_context(node: Mapping[str, Any], ctx: _Context) -> _Context:
    """Pick up system, collection and timestamp hints declared by ``node``."""
    updates: dict[str, str] = {}
    for key in ("data_store", "origin_system"):
        if isinstance(node.get(key), str):
            updates["system"] = node[key]
    if isinstance(node.get("collection"), str):
        updates["collection"] = node["collection"]
    for key in ("exported_at", "captured_at", "recorded_at", "observed_at"):
        stamp = _to_utc_text(node.get(key))
        if stamp:
            updates["observed_at"] = stamp
    return replace(ctx, **updates) if updates else ctx


def _flatten_record(
    node: Mapping[str, Any], prefix: str = ""
) -> tuple[dict[str, Any], list[Any]]:
    """Flatten nested objects with dotted keys.

    Nested objects that carry their own ``resource_id`` are returned separately
    as child nodes instead of being merged into the parent.
    """
    flat: dict[str, Any] = {}
    children: list[Any] = []
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            if isinstance(value.get("resource_id"), str):
                children.append(value)
            else:
                sub_flat, sub_children = _flatten_record(value, f"{dotted}.")
                flat.update(sub_flat)
                children.extend(sub_children)
        elif isinstance(value, list):
            nested = [
                item
                for item in value
                if isinstance(item, Mapping)
                and isinstance(item.get("resource_id"), str)
            ]
            if nested:
                children.extend(nested)
                rest = [item for item in value if item not in nested]
                if rest:
                    flat[dotted] = rest
            else:
                flat[dotted] = value
        else:
            flat[dotted] = value
    return flat, children


def _walk_json(
    ef: EvidenceFile, node: Any, ctx: _Context, out: list[EvidenceRecord]
) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_json(ef, item, ctx, out)
        return
    if not isinstance(node, Mapping):
        return
    ctx = _derive_context(node, ctx)
    if isinstance(node.get("resource_id"), str):
        flat, children = _flatten_record(node)
        declared = (
            node.get("system") if isinstance(node.get("system"), str) else ctx.system
        )
        extra = {"collection": ctx.collection} if ctx.collection else None
        out.append(
            _make_record(
                ef,
                resource_id=node["resource_id"],
                raw_fields=flat,
                raw_text=_dump(node),
                declared_system=declared,
                fallback_observed_at=ctx.observed_at,
                extra=extra,
            )
        )
        for child in children:
            _walk_json(ef, child, ctx, out)
        return
    for key, child in node.items():
        if key == "service_exports" and isinstance(child, Mapping):
            for system, groups in child.items():
                _walk_json(ef, groups, replace(ctx, system=str(system)), out)
        elif isinstance(child, list):
            _walk_json(ef, child, replace(ctx, collection=str(key)), out)
        elif isinstance(child, Mapping):
            _walk_json(ef, child, ctx, out)


def extract_json(ef: EvidenceFile, path: Path) -> list[EvidenceRecord]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(
            f"{ef.file_id} ({ef.file_path}): invalid JSON: {exc}"
        ) from exc
    records: list[EvidenceRecord] = []
    _walk_json(ef, data, _Context(observed_at=format_utc(ef.captured_at)), records)
    return records


# ---------------------------------------------------------------------------
# TXT extractor
# ---------------------------------------------------------------------------

_TXT_COLUMNS = (
    "recorded_at",
    "system",
    "resource_id",
    "record_type",
    "result",
    "subject_reference",
)
_SNAPSHOT_RE = re.compile(r"^Snapshot:\s*(\S+)", re.IGNORECASE)


def extract_text(ef: EvidenceFile, path: Path) -> list[EvidenceRecord]:
    """Parse ``- time | system | resource | type | result | subject`` lines."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetError(f"{ef.file_id} ({ef.file_path}): unreadable: {exc}") from exc
    snapshot_at: str | None = None
    records: list[EvidenceRecord] = []
    for line in lines:
        header = _SNAPSHOT_RE.match(line.strip())
        if header:
            snapshot_at = _to_utc_text(header.group(1)) or snapshot_at
            continue
        if not line.startswith("- "):
            continue
        parts = [part.strip() for part in line[2:].split(" | ")]
        if len(parts) != len(_TXT_COLUMNS):
            continue
        raw = dict(zip(_TXT_COLUMNS, parts))
        records.append(
            _make_record(
                ef,
                resource_id=raw["resource_id"],
                raw_fields=raw,
                raw_text=line,
                declared_system=raw["system"],
                fallback_observed_at=snapshot_at,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Log extractor
# ---------------------------------------------------------------------------

_KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|[^\s"]*)')
_BRACKET_RE = re.compile(
    r"^\[(?P<level>[A-Za-z]+)\]\s+\[(?P<service>[^\]]+)\]\s+\[(?P<trace>[^\]]+)\]"
    r"\s+(?P<event>[\w.]+):\s*(?P<kv>.*)$"
)
_PIPE_RE = re.compile(
    r"^(?:\|\s+)?(?P<level>[A-Za-z]+)\s+\|\s+(?P<service>[^|]+?)\s+\|\s+(?P<trace>[^|]+?)"
    r"\s+\|\s+(?P<event>[^|]+?)\s+\|\s*(?P<kv>.*)$"
)
_SYSLOG_RE = re.compile(
    r"^<\d+>\d+\s+(?P<node>\S+)\s+(?P<service>\S+)\s+(?P<trace>\S+)\s+-\s+-\s+(?P<kv>.*)$"
)
_INT_KEYS = frozenset(
    {
        "rows",
        "http_status",
        "duration_ms",
        "wait_ms",
        "attempt",
        "total_hits",
        "shard",
        "took_ms",
        "seq",
        "latency_ms",
        "lag",
        "partition",
        "retry_in_ms",
        "target_count",
        "direct_identifiers",
    }
)
_LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "WARN": 2, "WARNING": 2, "ERROR": 3, "FATAL": 4}


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return raw


def _parse_kv(text: str) -> dict[str, str]:
    return {key: _unquote(raw) for key, raw in _KV_RE.findall(text)}


def _coerce(key: str, value: Any) -> Any:
    """Convert log text values to None/bool/int where the type is unambiguous."""
    if not isinstance(value, str):
        return value
    if value in ("", "null", "None"):
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    if key in _INT_KEYS and re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


@dataclass(frozen=True, slots=True)
class _LogEvent:
    timestamp: str
    fields: Mapping[str, Any]
    raw: str


def _parse_log_line(line: str) -> _LogEvent | None:
    """Parse one log line of any supported syntax; ``None`` when not a log line."""
    stripped = line.strip()
    if not stripped:
        return None
    head, _, rest = stripped.partition(" ")
    stamp = _to_utc_text(head)
    if stamp is None:
        return None
    rest = rest.strip()
    fields: dict[str, Any] = {}

    if rest.startswith("{"):
        try:
            payload = json.loads(rest)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        flat, _ = _flatten_record(payload)
        fields.update(flat)
        if "severity" in fields:
            fields["level"] = fields.pop("severity")
        fields.pop("logger", None)
    elif (match := _BRACKET_RE.match(rest)) or (match := _PIPE_RE.match(rest)):
        fields.update(
            level=match["level"].upper(),
            service=match["service"].strip(),
            trace_id=match["trace"].strip(),
            event=match["event"].strip(),
        )
        fields.update(_parse_kv(match["kv"]))
    elif match := _SYSLOG_RE.match(rest):
        fields.update(service=match["service"], trace_id=match["trace"])
        fields.update(_parse_kv(match["kv"]))
    else:
        first, _, remainder = rest.partition(" ")
        body = rest if "=" in first else remainder
        if "=" not in first and first:
            fields["level"] = first.upper()
        fields.update(_parse_kv(body))
        if "message" in fields:  # cloud runtime: message="k=\"v\" ..."
            inner = _parse_kv(str(fields.pop("message")))
            fields.update(inner)
        if "requestId" in fields:
            fields["trace_id"] = fields.pop("requestId")
        if "function" in fields and "service" not in fields:
            fields["service"] = fields.pop("function")

    coerced = {key: _coerce(key, value) for key, value in fields.items()}
    return _LogEvent(timestamp=stamp, fields=coerced, raw=stripped)


def _log_record(
    ef: EvidenceFile, events: Sequence[_LogEvent], resource_id: str
) -> EvidenceRecord | None:
    merged: dict[str, Any] = {}
    for event in events:
        for key, value in event.fields.items():
            if key in ("event", "level"):
                continue
            if value is not None or key not in merged:
                merged[key] = value
    merged["events"] = [str(e.fields["event"]) for e in events if e.fields.get("event")]
    levels = [str(e.fields.get("level", "")).upper() for e in events]
    merged["log_level"] = max(
        levels, key=lambda lv: _LEVEL_RANK.get(lv, -1), default=""
    )
    merged["started_at"] = events[0].timestamp
    if resource_id:
        merged["resource_id"] = resource_id
    if not resource_id and not any(
        collect_identifiers(merged)[k] for k in IDENTIFIER_FIELDS
    ):
        return None
    with_resource = [e for e in events if e.fields.get("resource_id") == resource_id]
    observed = (with_resource or events)[-1].timestamp
    declared = merged.get("system") or merged.get("service")
    return _make_record(
        ef,
        resource_id=resource_id,
        raw_fields=merged,
        raw_text="\n".join(e.raw for e in events),
        declared_system=declared if isinstance(declared, str) else None,
        fallback_observed_at=observed,
        extra={"observed_at": observed},
    )


def extract_log(ef: EvidenceFile, path: Path) -> list[EvidenceRecord]:
    """Merge the lines of each trace into one record; lines without a trace stand alone."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetError(f"{ef.file_id} ({ef.file_path}): unreadable: {exc}") from exc
    groups: dict[str, list[_LogEvent]] = {}
    for index, line in enumerate(lines):
        event = _parse_log_line(line)
        if event is None:
            continue
        trace = event.fields.get("trace_id")
        key = str(trace) if trace else f"__line_{index}"
        groups.setdefault(key, []).append(event)

    records: list[EvidenceRecord] = []
    for events in groups.values():
        resource_ids = list(
            dict.fromkeys(
                str(e.fields["resource_id"])
                for e in events
                if e.fields.get("resource_id")
            )
        )
        if len(resource_ids) <= 1:
            record = _log_record(ef, events, resource_ids[0] if resource_ids else "")
            if record:
                records.append(record)
            continue
        for resource_id in resource_ids:
            subset = [
                e for e in events if e.fields.get("resource_id") in (None, resource_id)
            ]
            record = _log_record(ef, subset, resource_id)
            if record:
                records.append(record)
    return records


# ---------------------------------------------------------------------------
# SQL extractor
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(
    r"^--\s*(Captured at|Database topology):\s*(.+?)\s*$", re.MULTILINE
)
_VOLATILE_COLUMNS = frozenset({"updated_at"})


def _deny_attach(action: int, *_: Any) -> int:
    """Authorizer: snapshots may define and fill tables but never touch other files."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _table_role(name: str) -> str:
    lowered = name.lower()
    if "replica" in lowered:
        return "replica"
    if lowered.endswith("_primary") or "_primary_" in lowered:
        return "primary"
    return "standalone"


def _load_sql_tables(ef: EvidenceFile, script: str) -> dict[str, list[dict[str, Any]]]:
    """Execute the snapshot in an isolated in-memory SQLite and read every table."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.set_authorizer(_deny_attach)
    try:
        connection.executescript(script)
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
            )
        ]
        tables: dict[str, list[dict[str, Any]]] = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = [
                dict(row) for row in connection.execute(f"SELECT * FROM {quoted}")
            ]
        return tables
    except sqlite3.Error as exc:
        raise DatasetError(f"{ef.file_id} ({ef.file_path}): SQL error: {exc}") from exc
    finally:
        connection.close()


def _sql_row_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Columns of a row, with a ``record_json`` document expanded into fields."""
    fields: dict[str, Any] = {k: v for k, v in row.items() if k != "record_json"}
    document = row.get("record_json")
    if isinstance(document, str):
        try:
            payload = json.loads(document)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping):
            flat, _ = _flatten_record(payload)
            fields.update(flat)
    return fields


def _payload_key(fields: Mapping[str, Any]) -> str:
    return _dump({k: v for k, v in fields.items() if k not in _VOLATILE_COLUMNS})


def _replication_drift(
    tables: Mapping[str, str],
    presence: Mapping[str, str],
) -> str:
    """Classify one resource: ``tables`` maps table -> role, ``presence`` table -> payload."""
    roles = set(tables.values())
    if "primary" not in roles or "replica" not in roles:
        return "NOT_APPLICABLE"
    primary_present = any(tables[t] == "primary" for t in presence)
    if not primary_present:
        return "REPLICA_ONLY"
    if any(
        t not in presence
        for t, role in tables.items()
        if role in ("primary", "replica")
    ):
        return "PRIMARY_ONLY"
    if len(set(presence.values())) > 1:
        return "CONTENT_DIFFERS"
    return "NONE"


def extract_sql(ef: EvidenceFile, path: Path) -> list[EvidenceRecord]:
    """One record per table row, annotated with the primary/replica comparison."""
    try:
        script = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetError(f"{ef.file_id} ({ef.file_path}): unreadable: {exc}") from exc
    header = {key: value for key, value in _HEADER_RE.findall(script)}
    topology = header.get("Database topology", "inventory")
    snapshot_at = _to_utc_text(header.get("Captured at"))

    tables = _load_sql_tables(ef, script)
    scope = tables.pop("snapshot_scope", [])
    if snapshot_at is None and scope:
        snapshot_at = _to_utc_text(scope[0].get("captured_at"))
    roles = {name: _table_role(name) for name in tables}

    # Each scope row states which system the snapshot covers; it is the only way
    # to tell "absent from the snapshot" apart from "not covered by it".
    records: list[EvidenceRecord] = []
    for row in scope:
        origin = row.get("origin_system")
        records.append(
            _make_record(
                ef,
                resource_id="",
                raw_fields=row,
                raw_text=_dump(row),
                declared_system=origin if isinstance(origin, str) else None,
                fallback_observed_at=_to_utc_text(row.get("captured_at"))
                or snapshot_at,
                extra={
                    "table": "snapshot_scope",
                    "table_role": "scope",
                    "topology": topology,
                },
            )
        )

    row_fields: list[tuple[str, dict[str, Any]]] = []
    presence: dict[str, dict[str, str]] = {}
    for table, rows in tables.items():
        for row in rows:
            fields = _sql_row_fields(row)
            row_fields.append((table, fields))
            resource_id = fields.get("resource_id")
            if isinstance(resource_id, str):
                presence.setdefault(resource_id, {})[table] = _payload_key(fields)

    for table, fields in row_fields:
        resource_id = fields.get("resource_id")
        if not isinstance(resource_id, str):
            continue
        where = presence[resource_id]
        declared = fields.get("origin_system")
        records.append(
            _make_record(
                ef,
                resource_id=resource_id,
                raw_fields=fields,
                raw_text=_dump(fields),
                declared_system=declared if isinstance(declared, str) else None,
                fallback_observed_at=snapshot_at,
                extra={
                    "table": table,
                    "table_role": roles[table],
                    "topology": topology,
                    "present_in_tables": sorted(where),
                    "replication_drift": _replication_drift(roles, where),
                },
            )
        )
    return records


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_EXTRACTORS: Mapping[str, Callable[[EvidenceFile, Path], list[EvidenceRecord]]] = {
    ".sql": extract_sql,
    ".json": extract_json,
    ".txt": extract_text,
    ".log": extract_log,
}


def is_structured(ef: EvidenceFile) -> bool:
    return Path(ef.file_path).suffix.lower() in STRUCTURED_SUFFIXES


def extract_structured_file(
    ef: EvidenceFile, dataset_dir: Path | str = DEFAULT_DATASET_DIR
) -> list[EvidenceRecord]:
    """Extract the records of one structured evidence file."""
    suffix = Path(ef.file_path).suffix.lower()
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        raise DatasetError(f"{ef.file_id}: no structured extractor for {suffix!r}")
    path = ef.resolve(Path(dataset_dir))
    if not path.is_file():
        raise DatasetError(f"{ef.file_id}: missing evidence file {path}")
    return extractor(ef, path)


def extract_structured_files(
    evidence_files: Iterable[EvidenceFile],
    dataset_dir: Path | str = DEFAULT_DATASET_DIR,
) -> list[EvidenceRecord]:
    """Extract every structured file in manifest order (multimodal files are skipped)."""
    records: list[EvidenceRecord] = []
    for ef in evidence_files:
        if is_structured(ef):
            records.extend(extract_structured_file(ef, dataset_dir))
    return records


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class EvidenceIndex:
    """Lookup of evidence records by email, phone, email hash, user id, resource id or system."""

    def __init__(self, records: Iterable[EvidenceRecord]) -> None:
        self._records: tuple[EvidenceRecord, ...] = tuple(records)
        self._email: dict[str, list[int]] = {}
        self._phone: dict[str, list[int]] = {}
        self._hash: dict[str, list[int]] = {}
        self._user: dict[str, list[int]] = {}
        self._resource: dict[str, list[int]] = {}
        self._system: dict[str, list[int]] = {}
        for position, record in enumerate(self._records):
            fields = record.fields
            self._system.setdefault(record.system_id, []).append(position)
            for value in fields.get("emails", ()):
                self._email.setdefault(value, []).append(position)
            for value in fields.get("phones", ()):
                self._phone.setdefault(value, []).append(position)
            for value in fields.get("email_hashes", ()):
                self._hash.setdefault(value, []).append(position)
            for value in fields.get("user_ids", ()):
                self._user.setdefault(value, []).append(position)
            if record.resource_id:
                self._resource.setdefault(record.resource_id, []).append(position)

    def __len__(self) -> int:
        return len(self._records)

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return self._records

    def _pick(self, positions: Iterable[int]) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records[i] for i in sorted(set(positions)))

    def by_email(self, email: str) -> tuple[EvidenceRecord, ...]:
        return self._pick(self._email.get(normalize_email(email), ()))

    def by_phone(self, phone: str) -> tuple[EvidenceRecord, ...]:
        return self._pick(self._phone.get(normalize_phone(phone), ()))

    def by_hash(self, email_hash: str) -> tuple[EvidenceRecord, ...]:
        return self._pick(self._hash.get(normalize_hash(email_hash), ()))

    def by_user_id(self, user_id: str) -> tuple[EvidenceRecord, ...]:
        return self._pick(self._user.get(user_id.strip(), ()))

    def by_resource_id(self, resource_id: str) -> tuple[EvidenceRecord, ...]:
        return self._pick(self._resource.get(resource_id.strip(), ()))

    def by_system(self, system_id: str) -> tuple[EvidenceRecord, ...]:
        return self._pick(self._system.get(system_id.strip(), ()))

    def by_identifier(self, value: str) -> tuple[EvidenceRecord, ...]:
        """Look up ``value`` as whichever identifier kind it resembles."""
        text = value.strip()
        positions: list[int] = []
        if "@" in text:
            positions += self._email.get(normalize_email(text), [])
        else:
            if _HASH_RE.fullmatch(text):
                positions += self._hash.get(normalize_hash(text), [])
            positions += self._user.get(text, [])
            positions += self._resource.get(text, [])
            phone = normalize_phone(text) if _PHONE_VALUE_RE.match(text) else ""
            if phone:
                positions += self._phone.get(phone, [])
        return self._pick(positions)

    def by_identity(self, identity: Identity) -> tuple[EvidenceRecord, ...]:
        """Records linked to any current or historical identifier of ``identity``."""
        positions: list[int] = list(self._user.get(identity.user_id, ()))
        for email in identity.emails:
            positions += self._email.get(normalize_email(email), ())
        for phone in identity.phones:
            positions += self._phone.get(normalize_phone(phone), ())
        for email_hash in identity.email_hashes:
            positions += self._hash.get(normalize_hash(email_hash), ())
        return self._pick(positions)

    def until(self, audit_at: datetime) -> EvidenceIndex:
        """Index restricted to records of files captured at or before ``audit_at``."""
        return EvidenceIndex(r for r in self._records if r.captured_at <= audit_at)
