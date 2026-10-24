"""Identity resolution for the deletion-audit pipeline.

``resolve(case)`` maps the email supplied with a request (which may be a
current *or* a historical address) to one ``user_id`` and rebuilds every
identifier the subject is known to have used at or before ``audit_at``:
emails, phones, names and email hashes, together with the ``audit_id`` values
that prove each historical value.

Design notes
------------
* Matching is exact on the normalized email (trimmed, case-folded). No fuzzy or
  name-based matching is performed, so look-alike decoys such as
  ``g.silva76.jr@...`` never resolve to ``g.silva76@...``.
* Names are *not* unique across users (decoys share display names). They are
  returned for completeness, but ``ResolvedIdentity.ambiguous_names`` lists the
  names that other users also carry so downstream matching can avoid using them
  as a sole link.
* Only information available at or before ``audit_at`` is used: a value
  introduced by an audit event after ``audit_at`` is excluded, and the value
  that was in force at ``audit_at`` is reconstructed from the event chain even
  when the current snapshot has since moved on.
* ``email_hash`` values for historical emails are derived with the same SHA-256
  construction that the current snapshot uses; derived hashes are only added
  when the snapshot verifiably follows that construction.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Iterable, Sequence

from models import Case, Identity
from parser import AuditEvent, Tables, UserIdentity, load_tables

IDENTITY_FIELDS: tuple[str, ...] = ("email", "phone", "name", "email_hash")


class IdentityResolutionError(LookupError):
    """Base class for failures to map a case to exactly one user."""


class UnresolvedIdentityError(IdentityResolutionError):
    """The request email does not belong to any known user at ``audit_at``."""


class AmbiguousIdentityError(IdentityResolutionError):
    """The request email maps to more than one user."""


# ---------------------------------------------------------------------------
# Normalization helpers (shared with downstream matching)
# ---------------------------------------------------------------------------


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_hash(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    """Digits-only form of a phone number (``+`` kept as the first character)."""
    digits = re.sub(r"\D", "", value)
    return f"+{digits}" if value.strip().startswith("+") else digits


def email_sha256(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResolvedIdentity(Identity):
    """``Identity`` plus provenance and ambiguity information.

    ``value_audit_ids`` maps ``(field, normalized value)`` to the ``audit_id``
    values of the events that introduced or retired that value; current values
    that never changed have no entry. ``current`` holds the values in force at
    ``audit_at``.
    """

    audit_at: datetime | None = None
    request_email: str = ""
    current: dict[str, str] = field(default_factory=dict)
    value_audit_ids: dict[tuple[str, str], tuple[str, ...]] = field(
        default_factory=dict
    )
    ambiguous_names: set[str] = field(default_factory=set)

    def audit_ids_for(self, field_name: str, value: str) -> tuple[str, ...]:
        key_value = (
            normalize_email(value)
            if field_name == "email"
            else normalize_hash(value) if field_name == "email_hash" else value.strip()
        )
        return self.value_audit_ids.get((field_name, key_value), ())


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _normalize(field_name: str, value: str) -> str:
    if field_name == "email":
        return normalize_email(value)
    if field_name == "email_hash":
        return normalize_hash(value)
    return value.strip()


@dataclass(frozen=True, slots=True)
class _Version:
    """One value of a field and the audit events that bound its lifetime."""

    value: str
    introduced_at: datetime | None  # None: in use since before the audit log
    introduced_by: str | None
    retired_at: datetime | None  # None: still in force in the snapshot
    retired_by: str | None


class IdentityResolver:
    """Resolves cases against one loaded ``Tables`` instance."""

    def __init__(self, tables: Tables) -> None:
        self._identities: dict[str, UserIdentity] = {
            i.user_id: i for i in tables.identities
        }
        self._events: dict[str, list[AuditEvent]] = defaultdict(list)
        for event in sorted(
            tables.audit_events, key=lambda e: (e.event_time, e.audit_id)
        ):
            self._events[event.user_id].append(event)

        self._name_owners: dict[str, set[str]] = defaultdict(set)
        for identity in tables.identities:
            self._name_owners[identity.name.strip().casefold()].add(identity.user_id)

        # Whether the snapshot's email_hash follows sha256(email); only then is
        # it sound to derive hashes of historical emails.
        self._hash_is_sha256 = all(
            normalize_hash(i.email_hash) == email_sha256(i.email)
            for i in tables.identities
        )

        # (normalized email) -> {user_id: earliest instant the email was in use}
        self._email_index: dict[str, dict[str, datetime | None]] = defaultdict(dict)
        for user_id in self._identities:
            for version in self._chain(user_id, "email"):
                key = normalize_email(version.value)
                known = self._email_index[key]
                if user_id not in known or _earlier(
                    version.introduced_at, known[user_id]
                ):
                    known[user_id] = version.introduced_at

    # -- public API ---------------------------------------------------------

    def resolve(self, case: Case) -> ResolvedIdentity:
        user_id = self._find_user(case.email, case.audit_at)
        return self._build(user_id, case.audit_at, case.email)

    def resolve_user(self, user_id: str, audit_at: datetime) -> ResolvedIdentity:
        """Rebuild the identifiers of a known ``user_id`` as of ``audit_at``."""
        if user_id not in self._identities:
            raise UnresolvedIdentityError(f"unknown user_id {user_id!r}")
        return self._build(user_id, audit_at, "")

    # -- internals ----------------------------------------------------------

    def _chain(self, user_id: str, field_name: str) -> list[_Version]:
        """Value history of ``field_name`` from the audit log and the snapshot."""
        current = getattr(self._identities[user_id], field_name)
        events = [
            e for e in self._events.get(user_id, ()) if e.field_name == field_name
        ]
        if not events:
            return [_Version(current, None, None, None, None)]
        versions = [
            _Version(
                events[0].old_value,
                None,
                None,
                events[0].event_time,
                events[0].audit_id,
            )
        ]
        for index, event in enumerate(events):
            following = events[index + 1] if index + 1 < len(events) else None
            versions.append(
                _Version(
                    event.new_value,
                    event.event_time,
                    event.audit_id,
                    following.event_time if following else None,
                    following.audit_id if following else None,
                )
            )
        return versions

    def _find_user(self, email: str, audit_at: datetime) -> str:
        key = normalize_email(email)
        owners = {
            user_id
            for user_id, introduced_at in self._email_index.get(key, {}).items()
            if introduced_at is None or introduced_at <= audit_at
        }
        if not owners:
            raise UnresolvedIdentityError(
                f"email {email!r} is not linked to any user at {audit_at.isoformat()}"
            )
        if len(owners) > 1:
            raise AmbiguousIdentityError(
                f"email {email!r} maps to several users: {sorted(owners)}"
            )
        return next(iter(owners))

    def _build(
        self, user_id: str, audit_at: datetime, request_email: str
    ) -> ResolvedIdentity:
        result = ResolvedIdentity(
            user_id=user_id, audit_at=audit_at, request_email=request_email
        )
        sets = {
            "email": result.emails,
            "phone": result.phones,
            "name": result.names,
            "email_hash": result.email_hashes,
        }
        provenance: dict[tuple[str, str], list[str]] = defaultdict(list)

        for field_name in IDENTITY_FIELDS:
            visible = [
                v
                for v in self._chain(user_id, field_name)
                if v.introduced_at is None or v.introduced_at <= audit_at
            ]
            result.current[field_name] = _normalize(field_name, visible[-1].value)
            for version in visible:
                value = _normalize(field_name, version.value)
                sets[field_name].add(value)
                # Only events at or before audit_at may be cited.
                if version.introduced_by:
                    _append_unique(
                        provenance[(field_name, value)], version.introduced_by
                    )
                if (
                    version.retired_by
                    and version.retired_at is not None
                    and version.retired_at <= audit_at
                ):
                    _append_unique(provenance[(field_name, value)], version.retired_by)

        if self._hash_is_sha256:
            # Hash of every visible historical email, even when the audit log
            # holds no explicit email_hash row for it.
            for email in sorted(result.emails):
                result.email_hashes.add(email_sha256(email))

        result.audit_ids = [
            e.audit_id
            for e in self._events.get(user_id, ())
            if e.event_time <= audit_at
        ]
        result.value_audit_ids = {key: tuple(ids) for key, ids in provenance.items()}
        result.ambiguous_names = {
            name
            for name in result.names
            if len(self._name_owners.get(name.casefold(), ())) > 1
        }
        return result


def _earlier(candidate: datetime | None, current: datetime | None) -> bool:
    """True when ``candidate`` precedes ``current`` (``None`` means "always")."""
    if candidate is None:
        return current is not None
    return current is not None and candidate < current


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _default_resolver() -> IdentityResolver:
    return IdentityResolver(load_tables())


def resolve(case: Case, tables: Tables | None = None) -> ResolvedIdentity:
    """Resolve ``case`` to an ``Identity``.

    With no ``tables`` the default dataset is loaded once and reused.
    """
    resolver = IdentityResolver(tables) if tables is not None else _default_resolver()
    return resolver.resolve(case)


def resolve_many(
    cases: Iterable[Case], tables: Tables | None = None
) -> list[ResolvedIdentity]:
    resolver = IdentityResolver(tables) if tables is not None else _default_resolver()
    return [resolver.resolve(case) for case in cases]


__all__: Sequence[str] = (
    "AmbiguousIdentityError",
    "IdentityResolutionError",
    "IdentityResolver",
    "ResolvedIdentity",
    "UnresolvedIdentityError",
    "email_sha256",
    "normalize_email",
    "normalize_hash",
    "normalize_phone",
    "resolve",
    "resolve_many",
)
