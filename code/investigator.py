"""Investigation and state reconstruction (TASK-07).

Given a resolved ``Identity`` and a case ``audit_at``, this module finds every
record and recreation risk (active data flow, restorable backup) linked to the
subject and reconstructs the state each one had at ``audit_at``.

Principles
----------
* Only information available at or before ``audit_at`` is used: evidence files
  captured later, observations timestamped later and activities later are all
  ignored.
* Workflow results are claims. A ``SUCCESS`` deletion/redaction/anonymization
  activity never sets a state on its own; a resource state is accepted only
  from evidence (the latest observation per resource wins). The only
  activity-derived states are the explicit status checks (``CHECK_DATA_FLOW``,
  ``CHECK_RESTORE_ACCESS``), which state the observed condition, and
  recreation events (connector sync / backup restore) that happen after the
  latest evidence of the downstream copy.
* Linking is grounded: current/historical identifiers, hashes, resource ids
  named by the data catalog or by linked screens, and recreation activity
  details. The one heuristic (anonymized analytics profiles carry no
  identifier) is isolated in ``_link_unattributed_analytics``.

Public API: ``investigate(identity, audit_at, ...) -> list[Resource]``.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from evidence_extractors import (
    EvidenceIndex,
    collect_identifiers,
    normalize_email,
    system_for_resource,
)
from models import (
    EvidenceRecord,
    Identity,
    ObservedState,
    Resource,
    RiskKind,
    TOMBSTONE_CATEGORY,
)
from parser import Activity, Tables, parse_utc

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

ACCOUNT = "account_service"
ANALYTICS = "analytics_platform"
BACKUP = "backup_service"
INTEGRATION = "integration_service"
SEARCH = "search_service"
SUPPORT = "support_platform"
EXPORT = "export_service"
SECURITY = "security_service"
LEGAL = "legal_service"
IDENTITY = "identity_service"

# Default data category (``deletion_rules.csv``) per owning system.
SYSTEM_CATEGORY: Mapping[str, str] = {
    ACCOUNT: "USER_PROFILE",
    SEARCH: "SEARCH_INDEX_ENTRY",
    SUPPORT: "SUPPORT_MESSAGE",
    ANALYTICS: "ANALYTICS_PROFILE",
    EXPORT: "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "cache_service": "CACHE_ENTRY",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "object_storage": "SUPPORT_ATTACHMENT",
    SECURITY: "SECURITY_AUDIT_LOG",
    LEGAL: "LEGAL_HOLD_RECORD",
    BACKUP: "BACKUP_ARCHIVE",
    IDENTITY: TOMBSTONE_CATEGORY,
}

# Deterministic output order of systems (matches the golden step ordering).
SYSTEM_ORDER: tuple[str, ...] = (
    ACCOUNT,
    SEARCH,
    SECURITY,
    LEGAL,
    IDENTITY,
    SUPPORT,
    "object_storage",
    ANALYTICS,
    "marketing_platform",
    EXPORT,
    "cache_service",
    "email_delivery_service",
    INTEGRATION,
    BACKUP,
)

_NULLISH = {"", "none", "null", "nan"}
_TRUE = {"1", "true", "yes", "y", "enabled", "on", "allowed"}
_FALSE = {"0", "false", "no", "n", "disabled", "off", "blocked", "denied"}

_RESULT_STATE: Mapping[str, ObservedState] = {
    "NOT_FOUND": ObservedState.ABSENT,
    "ABSENT": ObservedState.ABSENT,
    "DELETED": ObservedState.ABSENT,
    "FOUND": ObservedState.PRESENT,
    "PRESENT": ObservedState.PRESENT,
    "IDENTIFIABLE": ObservedState.PRESENT,
    "ANONYMIZED": ObservedState.ANONYMIZED,
    "REDACTED": ObservedState.REDACTED,
    "ACTIVE": ObservedState.ACTIVE,
    "ENABLED": ObservedState.ACTIVE,
    "DISABLED": ObservedState.DISABLED,
    "BLOCKED": ObservedState.BLOCKED,
}

_USER_ID_RE = re.compile(r"(?<![A-Za-z0-9_])user_\d+(?![A-Za-z0-9_])")
_RESOURCE_ID_RE = re.compile(
    r"\b(?:account|analytics|audit_record|backup|cache|data_flow|email_delivery|"
    r"export|legal_hold|marketing|object|search|support|tombstone)_\d+\b"
)
_DOWNSTREAM_RE = re.compile(r"downstream resource\s+(\w+)")

# Second-of-minute fingerprint of anonymization events relative to the account
# deletion of the same subject in the activity stream (see
# ``_link_unattributed_analytics``).
_ANON_SECOND_OFFSETS = frozenset({40, 41, 42})
_ANON_MAX_DELAY = timedelta(hours=6)
_REPORT_WINDOW_BEFORE = timedelta(minutes=-90)
_REPORT_WINDOW_AFTER = timedelta(minutes=20)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InvestigatedResource(Resource):
    """``Resource`` plus the full proof trail of the reconstruction.

    ``evidence_id`` is the deterministic primary proof (the latest observation
    that establishes ``observed_state``); ``evidence_ids`` lists every
    participant-visible id that supports the linking or the state.
    """

    evidence_ids: tuple[str, ...] = ()
    observed_at: datetime | None = None
    recreated: bool = False
    notes: tuple[str, ...] = ()


@dataclass(slots=True)
class _Observation:
    state: ObservedState
    category: str | None
    at: datetime
    evidence_id: str
    order: int
    from_activity: bool = False
    replica_only: bool = False
    channel: str = "primary"


@dataclass(slots=True)
class _Candidate:
    system_id: str
    resource_id: str
    observations: list[_Observation] = field(default_factory=list)
    support_ids: set[str] = field(default_factory=set)
    depends_on: set[str] = field(default_factory=set)
    notes: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _NULLISH
    return False


def _truthy(value: Any) -> bool | None:
    """Interpret ``value`` as a boolean flag, or ``None`` when it is not one."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None


def _first(fields: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in fields and not _is_null(fields[key]):
            return fields[key]
    return None


def _has_key(fields: Mapping[str, Any], *keys: str) -> bool:
    return any(key in fields for key in keys)


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _attributes(fields: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = fields.get("attributes")
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _observed_at(record: EvidenceRecord) -> datetime:
    value = record.fields.get("observed_at")
    if isinstance(value, str):
        try:
            return parse_utc(value)
        except ValueError:
            pass
    return record.captured_at


# ---------------------------------------------------------------------------
# State extraction from one evidence record
# ---------------------------------------------------------------------------

_DIRECT_KEYS = ("email", "phone", "email_hash", "user_id", "name", "full_name")
_SUPPORT_PII_KEYS = (
    "requester_email",
    "requester_phone",
    "requester_name",
    "uploaded_by",
    "matched_value",
)


def _has_direct_identifier(fields: Mapping[str, Any]) -> bool:
    return any(not _is_null(fields.get(key)) for key in _DIRECT_KEYS)


def _generic_state(fields: Mapping[str, Any]) -> ObservedState | None:
    """State signals shared by every system (lookup results, HTTP codes...)."""
    for key in ("result", "visible_state"):
        value = fields.get(key)
        if isinstance(value, str) and value.strip().upper() in _RESULT_STATE:
            return _RESULT_STATE[value.strip().upper()]
    status = _as_int(fields.get("http_status") if "http_status" in fields else None)
    if status is not None:
        if status == 404:
            return ObservedState.ABSENT
        if 200 <= status < 300:
            return ObservedState.PRESENT
    rows = _as_int(fields.get("rows")) if "rows" in fields else None
    if rows is not None:
        return ObservedState.ABSENT if rows == 0 else ObservedState.PRESENT
    return None


def _analytics_state(fields: Mapping[str, Any]) -> ObservedState | None:
    state = _generic_state(fields)
    if state in (ObservedState.ANONYMIZED, ObservedState.ABSENT):
        return state
    if not _is_null(fields.get("matched_value")) or fields.get("detector"):
        return ObservedState.PRESENT
    count = (
        _as_int(fields.get("direct_identifiers"))
        if "direct_identifiers" in fields
        else None
    )
    if count is not None:
        return ObservedState.ANONYMIZED if count == 0 else ObservedState.PRESENT
    if _has_direct_identifier(fields) or not _is_null(fields.get("lookup_key")):
        return ObservedState.PRESENT
    if state is not None:
        return state
    keys_seen = _has_key(
        fields,
        "email",
        "phone",
        "email_hash",
        "user_id",
        "customer_reference",
        "audience_cohort",
        "metrics.sessions_30d",
    )
    if keys_seen:
        # every identifier column present and empty -> no longer attributable
        return ObservedState.ANONYMIZED
    return None


def _support_state(fields: Mapping[str, Any]) -> ObservedState | None:
    state = _generic_state(fields)
    if state is not None:
        return state
    if not _is_null(fields.get("matched_value")) or fields.get("detector"):
        return ObservedState.PRESENT
    text = fields.get("message_text")
    if isinstance(text, str) and not _is_null(text):
        if "[redacted]" in text.lower() and not _text_has_identifier(text):
            return ObservedState.REDACTED
        return (
            ObservedState.PRESENT
            if _text_has_identifier(text)
            else ObservedState.REDACTED
        )
    if any(not _is_null(fields.get(key)) for key in _SUPPORT_PII_KEYS):
        return ObservedState.PRESENT
    if _has_key(
        fields, "requester_email", "message_excerpt", "ticket_id", "file_name", "file"
    ):
        return (
            ObservedState.REDACTED
            if not _has_direct_identifier(fields)
            else ObservedState.PRESENT
        )
    return None


def _text_has_identifier(text: str) -> bool:
    found = collect_identifiers({"message_text": text})
    return bool(found["emails"] or found["phones"] or found["email_hashes"])


def _backup_state(fields: Mapping[str, Any]) -> ObservedState | None:
    state = _generic_state(fields)
    if state in (ObservedState.ACTIVE, ObservedState.BLOCKED):
        return state
    allowed = (
        _truthy(fields.get("restore_allowed")) if "restore_allowed" in fields else None
    )
    if allowed is not None:
        return ObservedState.ACTIVE if allowed else ObservedState.BLOCKED
    decision = fields.get("decision")
    if isinstance(decision, str) and decision.strip().lower() in {
        "deny",
        "denied",
        "block",
    }:
        return ObservedState.BLOCKED
    block = (
        _truthy(fields.get("deletion_block")) if "deletion_block" in fields else None
    )
    eligible = _truthy(fields.get("eligible")) if "eligible" in fields else None
    if block is True:
        return ObservedState.BLOCKED
    if eligible is True and block is False:
        return ObservedState.ACTIVE
    attributes = _attributes(fields)
    for key, value in attributes.items():
        if "restore" in str(key).lower():
            flag = _truthy(value)
            if flag is False or str(value).strip().lower() in {"blocked", "denied"}:
                return ObservedState.BLOCKED
            return ObservedState.ACTIVE
    return state


def _flow_state(fields: Mapping[str, Any]) -> ObservedState | None:
    state = _generic_state(fields)
    if state in (ObservedState.ACTIVE, ObservedState.DISABLED):
        return state
    enabled = _truthy(fields.get("enabled")) if "enabled" in fields else None
    if enabled is not None:
        return ObservedState.ACTIVE if enabled else ObservedState.DISABLED
    if str(fields.get("last_delivery_status", "")).strip().lower() == "stopped":
        return ObservedState.DISABLED
    return state


def _export_state(fields: Mapping[str, Any]) -> ObservedState | None:
    state = _generic_state(fields)
    if state is not None:
        return state
    for key in ("available", "download_available"):
        if key in fields:
            flag = _truthy(fields[key])
            if flag is not None:
                return ObservedState.PRESENT if flag else ObservedState.ABSENT
    return (
        ObservedState.PRESENT
        if _has_key(fields, "file_name", "file", "owner_email", "object_key")
        else None
    )


def _tombstone_state(fields: Mapping[str, Any]) -> ObservedState | None:
    """PRESENT means direct identifiers are still exposed; REDACTED means clean."""
    state = _generic_state(fields)
    if state is ObservedState.ABSENT:
        return state
    keys = [
        k for k in ("name", "full_name", "email", "phone", "email_hash") if k in fields
    ]
    if any(not _is_null(fields.get(k)) for k in keys):
        return ObservedState.PRESENT
    if keys or _has_key(
        fields, "subject_reference", "account_status", "collection_name"
    ):
        return ObservedState.REDACTED
    return state


def _record_state(system_id: str, fields: Mapping[str, Any]) -> ObservedState | None:
    if system_id == ANALYTICS:
        return _analytics_state(fields)
    if system_id == SUPPORT:
        return _support_state(fields)
    if system_id == BACKUP:
        return _backup_state(fields)
    if system_id == INTEGRATION:
        return _flow_state(fields)
    if system_id == EXPORT:
        return _export_state(fields)
    if system_id == IDENTITY:
        return _tombstone_state(fields)
    state = _generic_state(fields)
    if state is not None:
        return state
    if str(fields.get("replication_drift", "")).upper() == "REPLICA_ONLY":
        return ObservedState.PRESENT
    if system_id == SEARCH:
        hit = fields.get("result")
        if hit is not None and str(hit).lower() == "miss":
            return None
        if _has_key(
            fields, "document.email", "index_name", "index", "email", "match_key"
        ):
            return ObservedState.PRESENT
        return None
    if system_id in (SECURITY, LEGAL):
        if _has_key(
            fields,
            "event_name",
            "event_code",
            "retention_reason",
            "retention_until",
            "integrity_check",
            "subject_reference",
            "subject_ref",
            "order_reference",
        ):
            return ObservedState.PRESENT
        return None
    if system_id == ACCOUNT:
        if _has_direct_identifier(fields) or _has_key(
            fields, "account_status", "customer_reference"
        ):
            return ObservedState.PRESENT
        return None
    if _has_direct_identifier(fields):
        return ObservedState.PRESENT
    return None


def _category(system_id: str, fields: Mapping[str, Any]) -> str | None:
    if system_id == SUPPORT:
        # A contact-index lookup is a secondary identifier, not a conversation.
        if "contact_query" in str(fields.get("events", "")) or "contact_index" in str(
            fields.get("source", "")
        ):
            return "SECONDARY_IDENTIFIER"
        # A business document artifact (not a message thread) is an attachment.
        if not _is_null(fields.get("document_type")):
            return "SUPPORT_ATTACHMENT"
        attachment = (
            _has_key(fields, "uploaded_by", "storage_class")
            or "attachment" in str(fields.get("collection_name", "")).lower()
            or "attachment" in str(fields.get("collection", "")).lower()
            or "attachment" in str(fields.get("record_type", "")).lower()
            or "attachment" in str(fields.get("events", "")).lower()
            or (
                _has_key(fields, "file", "file_name")
                and not _has_key(fields, "message_text")
            )
        )
        return "SUPPORT_ATTACHMENT" if attachment else "SUPPORT_MESSAGE"
    if system_id == ANALYTICS:
        text = str(fields.get("events", "")).lower()
        if "contact_lookup" in text or _has_key(fields, "email_opt_in"):
            return "MARKETING_PROFILE"
        return "ANALYTICS_PROFILE"
    return SYSTEM_CATEGORY.get(system_id)


def _risk_kind(system_id: str) -> RiskKind | None:
    if system_id == INTEGRATION:
        return RiskKind.DATA_FLOW
    if system_id == BACKUP:
        return RiskKind.RESTORE
    return None


# ---------------------------------------------------------------------------
# Investigator
# ---------------------------------------------------------------------------


class Investigator:
    """Reconstructs resource states for subjects from tables and evidence."""

    def __init__(self, tables: Tables, index: EvidenceIndex) -> None:
        self._tables = tables
        self._index = index
        self._file_order = {ef.file_id: i for i, ef in enumerate(tables.evidence_files)}
        self._channel = {
            ef.file_id: ("replica" if "replica" in ef.origin.lower() else "primary")
            for ef in tables.evidence_files
        }
        self._activities_by_resource: dict[str, list[Activity]] = defaultdict(list)
        for activity in sorted(
            tables.activities, key=lambda a: (a.event_time, a.activity_id)
        ):
            self._activities_by_resource[activity.resource_id].append(activity)
        self._anon_candidates: list[EvidenceRecord] | None = None

    # -- public ------------------------------------------------------------

    def investigate(
        self,
        identity: Identity,
        audit_at: datetime,
        *,
        reported_at: datetime | None = None,
    ) -> list[InvestigatedResource]:
        index = self._index
        visible = self._visible_records(index, audit_at)
        activities = [a for a in self._tables.activities if a.event_time <= audit_at]

        candidates: dict[tuple[str, str], _Candidate] = {}
        linked_ids: set[str] = set()

        direct = [r for r in index.by_identity(identity) if self._usable(r, audit_at)]
        for record in direct:
            self._add_record(candidates, record, audit_at)
            linked_ids.update(self._linked_resource_ids(record))
        file_ids = {r.evidence_id for r in direct if "screen_state" in r.fields}

        catalog_ids = self._catalog_links(identity, activities)
        linked_ids.update(catalog_ids)
        for record in visible:
            if (
                record.evidence_id in file_ids
                and record.resource_id
                and not self._foreign(record, identity)
            ):
                linked_ids.add(record.resource_id)

        for resource_id in sorted(linked_ids):
            for record in index.by_resource_id(resource_id):
                if self._usable(record, audit_at) and not self._foreign(
                    record, identity
                ):
                    self._add_record(candidates, record, audit_at)

        for record in self._link_unattributed_analytics(
            identity, candidates, index, activities, audit_at
        ):
            self._add_record(candidates, record, audit_at)

        self._link_account_by_report_time(
            candidates, index, activities, audit_at, reported_at
        )
        self._apply_activities(candidates, activities, index, audit_at)
        self._observe_account_absence(candidates, index, audit_at)
        return self._finalize(candidates)

    # -- evidence filtering ---------------------------------------------------

    def _usable(self, record: EvidenceRecord, audit_at: datetime) -> bool:
        # The state is read at the observation time; a file captured after
        # ``audit_at`` may still describe the state before it, but it is never cited.
        return (
            bool(record.resource_id)
            and bool(system_for_resource(record.resource_id))
            and _observed_at(record) <= audit_at
        )

    def _visible_records(
        self, index: EvidenceIndex, audit_at: datetime
    ) -> list[EvidenceRecord]:
        return [r for r in index.records if self._usable(r, audit_at)]

    @staticmethod
    def _foreign(record: EvidenceRecord, identity: Identity) -> bool:
        """True when the record names someone, and none of those names is the subject."""
        fields = record.fields
        mine = (
            {normalize_email(e) for e in identity.emails},
            set(identity.phones),
            set(identity.email_hashes),
            {identity.user_id},
        )
        theirs = (
            set(fields.get("emails") or ()),
            set(fields.get("phones") or ()),
            set(fields.get("email_hashes") or ()),
            set(fields.get("user_ids") or ()),
        )
        named = any(theirs)
        return named and not any(a & b for a, b in zip(mine, theirs))

    @staticmethod
    def _linked_resource_ids(record: EvidenceRecord) -> set[str]:
        raw = record.fields.get("linked_resources")
        items: Iterable[Any]
        if isinstance(raw, str):
            try:
                items = json.loads(raw)
            except ValueError:
                items = _RESOURCE_ID_RE.findall(raw)
        elif isinstance(raw, Sequence):
            items = raw
        else:
            items = ()
        return {
            str(i) for i in items if isinstance(i, str) and _RESOURCE_ID_RE.fullmatch(i)
        }

    # -- candidate construction ------------------------------------------------

    def _add_record(
        self,
        candidates: dict[tuple[str, str], _Candidate],
        record: EvidenceRecord,
        audit_at: datetime,
    ) -> None:
        system_id = system_for_resource(record.resource_id)
        if not system_id:
            return
        key = (system_id, record.resource_id)
        candidate = candidates.setdefault(
            key, _Candidate(system_id, record.resource_id)
        )
        cited = record.evidence_id if record.captured_at <= audit_at else ""
        if cited:
            candidate.support_ids.add(cited)
        state = _record_state(system_id, record.fields)
        if state is None:
            return
        replica_only = (
            str(record.fields.get("replication_drift", "")).upper() == "REPLICA_ONLY"
        )
        if replica_only:
            candidate.notes.add("replica_only")
        candidate.observations.append(
            _Observation(
                state=state,
                category=_category(system_id, record.fields),
                at=_observed_at(record),
                evidence_id=cited,
                order=self._file_order.get(record.evidence_id, 1 << 30),
                replica_only=replica_only,
                channel=self._channel.get(record.evidence_id, "primary"),
            )
        )

    def _catalog_links(
        self, identity: Identity, activities: Sequence[Activity]
    ) -> set[str]:
        """Resource ids explicitly attached to the subject by the data catalog."""
        emails = {normalize_email(e) for e in identity.emails}
        linked: set[str] = set()
        for activity in activities:
            if activity.event_type != "LINK_RESOURCE_IDENTITY":
                continue
            found_emails = {
                normalize_email(m)
                for m in collect_identifiers({"d": activity.details})["emails"]
            }
            found_users = set(_USER_ID_RE.findall(activity.details))
            if found_emails & emails or identity.user_id in found_users:
                linked.add(activity.resource_id)
        return linked

    # -- unattributed analytics ----------------------------------------------

    def _anonymized_analytics(self, index: EvidenceIndex) -> list[EvidenceRecord]:
        found: list[EvidenceRecord] = []
        for record in index.by_system(ANALYTICS):
            if not record.resource_id.startswith("analytics_"):
                continue
            fields = record.fields
            if any(
                fields.get(k)
                for k in ("emails", "phones", "email_hashes", "user_ids", "names")
            ):
                continue
            if _analytics_state(fields) is ObservedState.ANONYMIZED:
                found.append(record)
        return found

    def _link_unattributed_analytics(
        self,
        identity: Identity,
        candidates: Mapping[tuple[str, str], _Candidate],
        index: EvidenceIndex,
        activities: Sequence[Activity],
        audit_at: datetime,
    ) -> list[EvidenceRecord]:
        """Attach anonymized analytics profiles (no identifier) to the subject.

        An anonymized profile holds nothing that names its owner, so the only
        available link is the activity stream. The worker actions of one
        subject occur at a stable second-of-minute offset from that subject's
        account-deletion activity (anonymization: +40..+42 s). A profile is
        attributed when its ``ANONYMIZE_RECORD`` activity follows the
        subject's account ``DELETE_RECORD`` within a few hours at that offset
        and no other identity claims it. This is a best-effort heuristic.
        """
        account_deletes = [
            a.event_time
            for (system_id, resource_id), _ in candidates.items()
            if system_id == ACCOUNT
            for a in self._activities_by_resource.get(resource_id, ())
            if a.event_type == "DELETE_RECORD"
            and a.target_system == ACCOUNT
            and a.event_time <= audit_at
        ]
        if not account_deletes:
            return []
        claimed = {rid for (system_id, rid) in candidates if system_id == ANALYTICS}
        selected: dict[str, tuple[timedelta, EvidenceRecord]] = {}
        for record in self._anonymized_analytics(index):
            rid = record.resource_id
            if rid in claimed or not self._usable(record, audit_at):
                continue
            for activity in self._activities_by_resource.get(rid, ()):
                if (
                    activity.event_type != "ANONYMIZE_RECORD"
                    or activity.event_time > audit_at
                ):
                    continue
                for deleted_at in account_deletes:
                    delay = activity.event_time - deleted_at
                    if timedelta(0) < delay <= _ANON_MAX_DELAY and (
                        int(delay.total_seconds()) % 60 in _ANON_SECOND_OFFSETS
                    ):
                        best = selected.get(rid)
                        if best is None or delay < best[0]:
                            selected[rid] = (delay, record)
        records: list[EvidenceRecord] = []
        if selected:
            # keep the analytics profile closest to the deletion when several fit
            _, record = min(
                selected.values(), key=lambda item: (item[0], item[1].resource_id)
            )
            same = [r for r in index.by_resource_id(record.resource_id)]
            records.extend(same)
        return records

    # -- account discovery and absence ---------------------------------------

    def _link_account_by_report_time(
        self,
        candidates: dict[tuple[str, str], _Candidate],
        index: EvidenceIndex,
        activities: Sequence[Activity],
        audit_at: datetime,
        reported_at: datetime | None,
    ) -> None:
        """Best effort: attach the account whose deletion the workflow reported.

        A deleted account leaves no identifier in any file, so when no account
        is linked yet the only handle is the activity stream. The worker
        deletion of the account runs close to the time the workflow reported
        its status (``reported_at``). The account is attached only when its
        ``DELETE_RECORD`` is the single one inside that window; otherwise the
        match would be a guess and is skipped.
        """
        if reported_at is None or any(system == ACCOUNT for system, _ in candidates):
            return
        window = [
            a
            for a in activities
            if a.event_type == "DELETE_RECORD"
            and a.target_system == ACCOUNT
            and _REPORT_WINDOW_BEFORE
            <= a.event_time - reported_at
            <= _REPORT_WINDOW_AFTER
        ]
        if len(window) != 1:
            return
        resource_id = window[0].resource_id
        candidates[(ACCOUNT, resource_id)] = _Candidate(ACCOUNT, resource_id)
        for record in index.by_resource_id(resource_id):
            if self._usable(record, audit_at):
                self._add_record(candidates, record, audit_at)

    def _observe_account_absence(
        self,
        candidates: dict[tuple[str, str], _Candidate],
        index: EvidenceIndex,
        audit_at: datetime,
    ) -> None:
        """Absence proof: a later account-system capture that does not hold the account.

        After an account deletion, the latest capture covering the account
        system (database export, snapshot bundle, readback log) taken by
        ``audit_at`` shows the state of that system. If the capture holds no
        record of the subject's account, the account is absent at that time.
        """
        accounts = [(k, c) for k, c in candidates.items() if k[0] == ACCOUNT]
        if not accounts:
            return
        coverage: dict[str, datetime] = {}
        for record in index.by_system(ACCOUNT):
            if record.captured_at <= audit_at:
                coverage[record.evidence_id] = record.captured_at
        for (_, resource_id), candidate in accounts:
            deleted_at = max(
                (
                    a.event_time
                    for a in self._activities_by_resource.get(resource_id, ())
                    if a.event_type == "DELETE_RECORD" and a.event_time <= audit_at
                ),
                default=None,
            )
            if deleted_at is None:
                continue
            holding = {o.evidence_id for _, c in accounts for o in c.observations}
            for channel in ("primary", "replica"):
                eligible = [
                    (captured_at, file_id)
                    for file_id, captured_at in coverage.items()
                    if captured_at >= deleted_at
                    and file_id not in holding
                    and self._channel.get(file_id, "primary") == channel
                ]
                if not eligible:
                    continue
                captured_at, file_id = max(eligible)
                candidate.support_ids.add(file_id)
                candidate.observations.append(
                    _Observation(
                        state=ObservedState.ABSENT,
                        category="USER_PROFILE",
                        at=captured_at,
                        evidence_id=file_id,
                        order=self._file_order.get(file_id, 1 << 30),
                        channel=channel,
                    )
                )

    # -- activity-derived facts ------------------------------------------------

    def _apply_activities(
        self,
        candidates: dict[tuple[str, str], _Candidate],
        activities: Sequence[Activity],
        index: EvidenceIndex,
        audit_at: datetime,
    ) -> None:
        # explicit status checks (state is stated by the activity itself)
        for (system_id, resource_id), candidate in list(candidates.items()):
            for activity in self._activities_by_resource.get(resource_id, ()):
                if activity.event_time > audit_at:
                    break
                text = activity.details.lower()
                if (
                    activity.event_type == "CHECK_DATA_FLOW"
                    and "remains enabled" in text
                ):
                    self._observe(candidate, ObservedState.ACTIVE, activity)
                elif (
                    activity.event_type == "CHECK_RESTORE_ACCESS"
                    and "can still be restored" in text
                ):
                    self._observe(candidate, ObservedState.ACTIVE, activity)

        # recreation paths: active connectors and restores that repopulate copies
        for (system_id, resource_id), candidate in list(candidates.items()):
            if system_id not in (INTEGRATION, BACKUP):
                continue
            for activity in self._activities_by_resource.get(resource_id, ()):
                if activity.event_time > audit_at:
                    break
                if activity.event_type not in ("SYNC_RECORD", "RESTORE_BACKUP"):
                    continue
                match = _DOWNSTREAM_RE.search(activity.details)
                if not match:
                    continue
                downstream = match.group(1)
                candidate.depends_on.add(downstream)
                candidate.support_ids.add(activity.activity_id)
                down_system = system_for_resource(downstream, activity.target_system)
                if not down_system:
                    continue
                target = candidates.setdefault(
                    (down_system, downstream), _Candidate(down_system, downstream)
                )
                for record in index.by_resource_id(downstream):
                    if self._usable(record, audit_at):
                        self._add_record(candidates, record, audit_at)
                target.support_ids.add(activity.activity_id)
                latest = max((o.at for o in target.observations), default=None)
                if latest is None or latest < activity.event_time:
                    self._observe(
                        target, ObservedState.PRESENT, activity, recreated=True
                    )

        # replica recreation of the account record after its deletion
        for (system_id, resource_id), candidate in list(candidates.items()):
            if system_id != ACCOUNT:
                continue
            deletes = [
                a.event_time
                for a in self._activities_by_resource.get(resource_id, ())
                if a.event_type == "DELETE_RECORD" and a.event_time <= audit_at
            ]
            if not deletes:
                continue
            last_delete = max(deletes)
            for activity in self._activities_by_resource.get(resource_id, ()):
                if (
                    activity.event_type == "SYNC_RECORD"
                    and activity.target_system
                    in ("account_failover_replica", "account_replication_cluster")
                    and last_delete < activity.event_time <= audit_at
                ):
                    candidate.support_ids.add(activity.activity_id)
                    candidate.notes.add("replica_sync_after_delete")

    @staticmethod
    def _observe(
        candidate: _Candidate,
        state: ObservedState,
        activity: Activity,
        *,
        recreated: bool = False,
    ) -> None:
        candidate.observations.append(
            _Observation(
                state=state,
                category=None,
                at=activity.event_time,
                evidence_id=activity.activity_id,
                order=-1,
                from_activity=True,
            )
        )
        candidate.support_ids.add(activity.activity_id)
        if recreated:
            candidate.notes.add("recreated_by_" + activity.event_type.lower())

    # -- finalize ---------------------------------------------------------------

    def _finalize(
        self, candidates: Mapping[tuple[str, str], _Candidate]
    ) -> list[InvestigatedResource]:
        resources: list[InvestigatedResource] = []
        for (system_id, resource_id), candidate in candidates.items():
            chosen = self._choose(candidate)
            if chosen is None:
                continue
            category = chosen.category or next(
                (o.category for o in reversed(candidate.observations) if o.category),
                SYSTEM_CATEGORY.get(system_id),
            )
            risk = _risk_kind(system_id)
            # a recreation path that merely "exists" is an active path
            state = (
                ObservedState.ACTIVE
                if risk is not None and chosen.state is ObservedState.PRESENT
                else chosen.state
            )
            resources.append(
                InvestigatedResource(
                    system_id=system_id,
                    resource_id=resource_id,
                    data_category=category,
                    risk_kind=risk,
                    observed_state=state,
                    evidence_id=chosen.evidence_id,
                    depends_on=tuple(sorted(candidate.depends_on)),
                    evidence_ids=tuple(
                        sorted((candidate.support_ids | {chosen.evidence_id}) - {""})
                    ),
                    observed_at=chosen.at,
                    recreated=any(
                        n.startswith("recreated_by_") for n in candidate.notes
                    ),
                    notes=tuple(sorted(candidate.notes)),
                )
            )
        order = {name: i for i, name in enumerate(SYSTEM_ORDER)}
        resources.sort(
            key=lambda r: (
                order.get(r.system_id, len(order)),
                r.system_id,
                r.resource_id,
            )
        )
        return resources

    @staticmethod
    def _choose(candidate: _Candidate) -> _Observation | None:
        """Latest observation wins; on ties the state that keeps data present wins."""
        if not candidate.observations:
            return None
        rank = {
            ObservedState.PRESENT: 5,
            ObservedState.ACTIVE: 5,
            ObservedState.REDACTED: 3,
            ObservedState.ANONYMIZED: 3,
            ObservedState.BLOCKED: 2,
            ObservedState.DISABLED: 2,
            ObservedState.ABSENT: 1,
        }

        def best(observations: Iterable[_Observation]) -> _Observation:
            return max(
                observations,
                key=lambda o: (
                    o.at,
                    bool(o.evidence_id),
                    rank.get(o.state, 0),
                    o.order,
                    o.evidence_id,
                ),
            )

        if candidate.system_id == ACCOUNT:
            # A replica is an independent copy: a later clean primary does not
            # remove a record that the replica still holds (replication drift).
            latest = [
                best([o for o in candidate.observations if o.channel == channel])
                for channel in {o.channel for o in candidate.observations}
            ]
            holding = [o for o in latest if o.state is ObservedState.PRESENT]
            if holding:
                return best(holding)
        return best(candidate.observations)


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------

_DEFAULT: Investigator | None = None


def _default_investigator() -> Investigator:
    global _DEFAULT
    if _DEFAULT is None:
        from llm import LLMClient
        from parser import (
            build_evidence_index,
            extract_multimodal,
            extract_structured,
            load_tables,
        )

        tables = load_tables()
        records = extract_structured(tables) + extract_multimodal(tables, LLMClient())
        _DEFAULT = Investigator(tables, build_evidence_index(records))
    return _DEFAULT


def investigate(
    identity: Identity,
    audit_at: datetime,
    *,
    reported_at: datetime | None = None,
    tables: Tables | None = None,
    index: EvidenceIndex | None = None,
) -> list[Resource]:
    """Resources linked to ``identity`` with their reconstructed state at ``audit_at``.

    Pass ``tables`` and ``index`` to reuse already loaded data; otherwise the
    default dataset (including cached multimodal answers) is loaded once.
    """
    if tables is not None and index is not None:
        investigator = Investigator(tables, index)
    else:
        investigator = _default_investigator()
    return list(investigator.investigate(identity, audit_at, reported_at=reported_at))
