"""Three-pass, bounded-memory FHIR NDJSON quality gate with SQLite reference index."""
from __future__ import annotations
import contextlib
import gzip
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from .models import Issue, Summary
from .outcome import operation_outcome
from .readiness import assess_readiness
from .rules import Policy, iter_references, iter_reference_values, local_issues, relative_key, is_versioned_reference, strict_json_loads, _date_prefix, ID_RE, TYPE_RE


@dataclass(frozen=True)
class Record:
    index: int
    resource: Any
    full_url: str | None = None
    parse_error: bool = False
    bundle_id: int = 0
    source_file_index: int = 0  # 1-based ordinal, never a PHI-bearing filesystem path
    source_line: int = 0        # physical NDJSON line (Bundles expand to multiple records)
    source_entry: int | None = None  # 0-based Bundle.entry index, when applicable


def _reader(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open("rt", encoding="utf-8")


MAX_LINE_CHARS = 8 * 1024 * 1024


def records_from_ndjson(paths: list[Path]) -> Iterator[Record]:
    """Yield each resource; unpack Bundle.entry with fullUrl for local URN resolution.

    Bad JSON becomes a quarantinable marker without ever putting raw PHI into errors.
    """
    index = 0
    bundle_seq = 0
    for file_index, path in enumerate(paths, 1):
        with _reader(path) as file:
            line_number = 0
            while True:
                raw = file.readline(MAX_LINE_CHARS + 1)
                if not raw:
                    break
                line_number += 1
                if len(raw) > MAX_LINE_CHARS:
                    # Drain oversized lines in chunks, without retaining patient data.
                    while not raw.endswith("\n"):
                        raw = file.readline(65536)
                        if not raw:
                            break
                    index += 1
                    yield Record(index, None, parse_error=True, source_file_index=file_index, source_line=line_number)
                    continue
                if not raw.strip():
                    continue
                try:
                    parsed = strict_json_loads(raw)
                except (ValueError, TypeError, RecursionError):
                    index += 1
                    yield Record(index, None, parse_error=True, source_file_index=file_index, source_line=line_number)
                    continue
                if isinstance(parsed, dict) and parsed.get("resourceType") == "Bundle":
                    bundle_seq += 1
                    entries = parsed.get("entry")
                    if not isinstance(entries, list):
                        index += 1
                        yield Record(index, parsed, source_file_index=file_index, source_line=line_number)
                        continue
                    for entry_index, entry in enumerate(entries):
                        index += 1
                        if isinstance(entry, dict):
                            yield Record(index, entry.get("resource"), entry.get("fullUrl") if isinstance(entry.get("fullUrl"), str) else None, bundle_id=bundle_seq,
                                         source_file_index=file_index, source_line=line_number, source_entry=entry_index)
                        else:
                            yield Record(index, None, bundle_id=bundle_seq, source_file_index=file_index,
                                         source_line=line_number, source_entry=entry_index)
                else:
                    index += 1
                    yield Record(index, parsed, source_file_index=file_index, source_line=line_number)


def _create_private_file(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


class _Index:
    def __init__(self, db: sqlite3.Connection, secret: bytes, trusted_reference_bases: tuple[str, ...] = ()):
        self.db = db
        self.secret = secret
        self.trusted_reference_bases = trusted_reference_bases
        # Instance-local, bounded caches. References to the index are not shared
        # between runs and their PHI-bearing keys disappear with this index.
        # Populate the SQLite resource index *before* any cached read.
        self._exists_cached = lru_cache(maxsize=8192)(self._exists_uncached)
        self._subject_cached = lru_cache(maxsize=8192)(self._subject_uncached)
        self._birth_cached = lru_cache(maxsize=8192)(self._birth_uncached)
        self._rejected_cached = lru_cache(maxsize=8192)(self._target_rejected_uncached)
        db.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE resources (rtype TEXT NOT NULL, rid TEXT NOT NULL,
                subject TEXT, birth TEXT, bundle_id INTEGER, PRIMARY KEY(rtype, rid));
            CREATE TABLE duplicate_keys (rtype TEXT NOT NULL, rid TEXT NOT NULL,
                PRIMARY KEY(rtype, rid));
            CREATE TABLE aliases (bundle_id INTEGER, url TEXT, rtype TEXT, rid TEXT, PRIMARY KEY(bundle_id,url));
            CREATE TABLE duplicate_aliases (bundle_id INTEGER, url TEXT, PRIMARY KEY(bundle_id,url));
            CREATE TABLE identifiers (hash TEXT PRIMARY KEY, owner TEXT);
            CREATE TABLE duplicate_identifiers (hash TEXT PRIMARY KEY);
            CREATE TABLE rejected_keys (rtype TEXT NOT NULL, rid TEXT NOT NULL, PRIMARY KEY (rtype,rid));
            CREATE TABLE dependencies (child_type TEXT NOT NULL, child_id TEXT NOT NULL,
                                      target_type TEXT NOT NULL, target_id TEXT NOT NULL,
                                      PRIMARY KEY (child_type,child_id,target_type,target_id));
            CREATE INDEX dependencies_target ON dependencies (target_type,target_id);
        """)

    def add(self, rec: Record):
        r = rec.resource
        if not isinstance(r, dict):
            return
        typ, rid = r.get("resourceType"), r.get("id")
        if not isinstance(typ, str) or not isinstance(rid, str):
            return
        if not TYPE_RE.fullmatch(typ) or not ID_RE.fullmatch(rid):
            return
        subj = r.get("subject")
        subject = subj.get("reference") if isinstance(subj, dict) and isinstance(subj.get("reference"), str) else None
        try:
            self.db.execute("INSERT INTO resources VALUES (?, ?, ?, ?, ?)",
                            (typ, rid, subject, r.get("birthDate") if typ == "Patient" and isinstance(r.get("birthDate"), str) else None, rec.bundle_id))
        except sqlite3.IntegrityError:
            self.db.execute("INSERT OR IGNORE INTO duplicate_keys VALUES (?, ?)", (typ, rid))
        if rec.full_url:
            try:
                self.db.execute("INSERT INTO aliases VALUES (?, ?, ?, ?)", (rec.bundle_id, rec.full_url, typ, rid))
            except sqlite3.IntegrityError:
                self.db.execute("INSERT OR IGNORE INTO duplicate_aliases VALUES (?, ?)", (rec.bundle_id, rec.full_url))
        if typ == "Patient" and isinstance(r.get("identifier"), list):
            for identifier in r["identifier"]:
                if not isinstance(identifier, dict) or not isinstance(identifier.get("value"), str):
                    continue
                system = identifier.get("system", "")
                if not isinstance(system, str):
                    continue
                digest = hmac.new(self.secret, json.dumps([system, identifier["value"]], separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
                try:
                    self.db.execute("INSERT INTO identifiers VALUES (?, ?)", (digest, rid))
                except sqlite3.IntegrityError:
                    owner = self.db.execute("SELECT owner FROM identifiers WHERE hash = ?", (digest,)).fetchone()
                    if owner and owner[0] != rid:
                        self.db.execute("INSERT OR IGNORE INTO duplicate_identifiers VALUES (?)", (digest,))

    def plan_dependency(self, resource: dict, bundle_id: int):
        typ, rid = resource.get("resourceType"), resource.get("id")
        if not isinstance(typ, str) or not TYPE_RE.fullmatch(typ) or not isinstance(rid, str) or not ID_RE.fullmatch(rid):
            return
        for ref in iter_reference_values(resource):
            target = self.resolve(ref, bundle_id)
            if target is not None and target != (typ, rid):
                self.db.execute("INSERT OR IGNORE INTO dependencies VALUES (?,?,?,?)", (typ, rid, *target))

    def mark_rejected(self, typ: str, rid: str):
        self.db.execute("INSERT OR IGNORE INTO rejected_keys VALUES (?,?)", (typ, rid))

    def close_rejections(self):
        """Compute the transitive set of resources depending on rejected keys.

        Recursive UNION eliminates cycles. A bounded set-based SQL traversal is
        preferable to repeatedly rescanning files for long reference chains.
        """
        self.db.execute("""
            INSERT OR IGNORE INTO rejected_keys (rtype,rid)
            WITH RECURSIVE impacted (rtype,rid) AS (
              SELECT rtype,rid FROM rejected_keys
              UNION
              SELECT d.child_type,d.child_id FROM dependencies d
              JOIN impacted i ON i.rtype=d.target_type AND i.rid=d.target_id
            )
            SELECT rtype,rid FROM impacted
        """)

    def target_rejected(self, typ: str, rid: str) -> bool:
        # Only read in the third pass, after close_rejections() commits the graph.
        return self._rejected_cached(typ, rid)

    def _target_rejected_uncached(self, typ: str, rid: str) -> bool:
        return self.db.execute("SELECT 1 FROM rejected_keys WHERE rtype=? AND rid=?", (typ,rid)).fetchone() is not None

    def duplicate_id(self, typ: str, rid: str) -> bool:
        return self.db.execute("SELECT 1 FROM duplicate_keys WHERE rtype=? AND rid=?", (typ, rid)).fetchone() is not None

    def duplicate_identifiers(self, r: dict) -> bool:
        if r.get("resourceType") != "Patient" or not isinstance(r.get("identifier"), list):
            return False
        for ident in r["identifier"]:
            if isinstance(ident, dict) and isinstance(ident.get("value"), str) and isinstance(ident.get("system", ""), str):
                raw = json.dumps([ident.get("system", ""), ident["value"]], separators=(",", ":")).encode()
                digest = hmac.new(self.secret, raw, hashlib.sha256).hexdigest()
                if self.db.execute("SELECT 1 FROM duplicate_identifiers WHERE hash=?", (digest,)).fetchone():
                    return True
        return False

    def resolve(self, reference: str, bundle_id: int = 0) -> tuple[str, str] | None:
        relative = relative_key(reference)
        if relative:
            return relative
        if reference.startswith("#"):
            return None
        # Bundle aliases only exist for bundle_id > 0. Regular bulk NDJSON
        # references need no alias query, saving two SQLite lookups per URL.
        if bundle_id:
            if self.db.execute("SELECT 1 FROM duplicate_aliases WHERE bundle_id=? AND url=?", (bundle_id, reference)).fetchone():
                return None
            row = self.db.execute("SELECT rtype,rid FROM aliases WHERE bundle_id=? AND url=?", (bundle_id, reference)).fetchone()
            if row:
                return (row[0], row[1])
        for base in self.trusted_reference_bases:
            # Exact boundary prevents matching a hostile sibling hostname or
            # path such as https://hospital.example/fhir-evil/Patient/x.
            if reference.startswith(base + "/"):
                return relative_key(reference[len(base) + 1:])
        return None

    def is_local_versioned_reference(self, ref: str) -> bool:
        # Local version-specific references cannot be checked against an index
        # that stores only the latest logical ID. They must not be accepted as
        # proof that the intended historical resource exists.
        if "/_history/" not in ref:
            return False
        if is_versioned_reference(ref):
            return True
        return any(ref.startswith(base + "/") and is_versioned_reference(ref[len(base) + 1:])
                   for base in self.trusted_reference_bases)

    def exists(self, typ: str, rid: str) -> bool:
        return self._exists_cached(typ, rid)

    def _exists_uncached(self, typ: str, rid: str) -> bool:
        return self.db.execute("SELECT 1 FROM resources WHERE rtype=? AND rid=?", (typ, rid)).fetchone() is not None

    def subject(self, typ: str, rid: str) -> str | None:
        return self._subject_cached(typ, rid)

    def _subject_uncached(self, typ: str, rid: str) -> str | None:
        row = self.db.execute("SELECT subject FROM resources WHERE rtype=? AND rid=?", (typ, rid)).fetchone()
        return row[0] if row else None

    def birth(self, rid: str) -> str | None:
        return self._birth_cached(rid)

    def _birth_uncached(self, rid: str) -> str | None:
        row = self.db.execute("SELECT birth FROM resources WHERE rtype='Patient' AND rid=?", (rid,)).fetchone()
        return row[0] if row else None

    def resource_bundle(self, typ: str, rid: str) -> int:
        row = self.db.execute("SELECT bundle_id FROM resources WHERE rtype=? AND rid=?", (typ, rid)).fetchone()
        return int(row[0]) if row else 0

    def ambiguous_alias(self, ref: str, bundle_id: int) -> bool:
        if not bundle_id:
            return False
        return self.db.execute("SELECT 1 FROM duplicate_aliases WHERE bundle_id=? AND url=?", (bundle_id, ref)).fetchone() is not None


def _reference_issues(r: Any, idx: _Index, policy: Policy, bundle_id: int = 0) -> list[Issue]:
    if not isinstance(r, dict):
        return []
    result = []
    contained = {x.get("id") for x in r.get("contained", []) if isinstance(x, dict) and isinstance(x.get("id"), str)} if isinstance(r.get("contained", []), list) else set()
    for path, ref in iter_references(r):
        if ref.startswith("#"):
            if ref[1:] not in contained:
                result.append(Issue("DQ_CONTAINED_REFERENCE_MISSING", "error", path, "Contained reference not found"))
            continue
        if idx.ambiguous_alias(ref, bundle_id):
            result.append(Issue("DQ_AMBIGUOUS_FULLURL", "error", path, "Bundle fullUrl is non-unique"))
            continue
        if idx.is_local_versioned_reference(ref):
            result.append(Issue("DQ_VERSION_REFERENCE_NOT_CHECKED", "warning", path,
                                "Version-specific reference requires a resource version index"))
            continue
        target = idx.resolve(ref, bundle_id)
        if target is None:
            result.append(Issue("DQ_REFERENCE_NOT_CHECKED", "warning", path, "External or conditional reference not checked against local dataset"))
        elif not idx.exists(*target):
            sev = "error" if policy.reference_scope == "closed" or target[0] in policy.complete_resource_types else "warning"
            result.append(Issue("DQ_REFERENCE_NOT_FOUND", sev, path, "Referenced resource not found in local dataset"))
    if policy.check_patient_consistency and r.get("resourceType") == "Observation":
        subject = (r.get("subject") or {}).get("reference") if isinstance(r.get("subject"), dict) else None
        encounter = (r.get("encounter") or {}).get("reference") if isinstance(r.get("encounter"), dict) else None
        patient_target = idx.resolve(subject, bundle_id) if isinstance(subject, str) else None
        encounter_target = idx.resolve(encounter, bundle_id) if isinstance(encounter, str) else None
        if encounter_target and encounter_target[0] == "Encounter" and idx.exists(*encounter_target):
            enc_subject = idx.subject(*encounter_target)
            resolved_enc_subject = idx.resolve(enc_subject, idx.resource_bundle(*encounter_target)) if enc_subject else None
            if patient_target and resolved_enc_subject and patient_target != resolved_enc_subject:
                result.append(Issue("DQ_PATIENT_ENCOUNTER_MISMATCH", "error", "/encounter", "Observation and encounter refer to different patients"))
        if patient_target and patient_target[0] == "Patient" and idx.exists(*patient_target):
            birth = _date_prefix(idx.birth(patient_target[1]))
            event = _date_prefix(r.get("effectiveDateTime"))
            if birth and event and event < birth:
                result.append(Issue("DQ_EVENT_BEFORE_BIRTH", "error", "/effectiveDateTime", "Observation precedes recorded patient birth date"))
    return result


def _record_issues(rec: Record, index: _Index, policy: Policy, *, propagate: bool = False) -> list[Issue]:
    r = rec.resource
    issues = ([Issue("FHIR_JSON_INVALID", "error", "", "Invalid JSON, duplicate key, or unsafe decimal precision")]
              if rec.parse_error else local_issues(r, policy))
    if isinstance(r, dict) and isinstance(r.get("resourceType"), str) and isinstance(r.get("id"), str):
        if index.duplicate_id(r["resourceType"], r["id"]):
            issues.append(Issue("DQ_DUPLICATE_LOGICAL_ID", "error", "/id", "Repeated resourceType/id in migration batch"))
        if index.duplicate_identifiers(r):
            issues.append(Issue("DQ_DUPLICATE_PATIENT_IDENTIFIER", "error", "/identifier", "Patient identifier reused by different patients"))
        issues.extend(_reference_issues(r, index, policy, rec.bundle_id))
        if propagate:
            for path, ref in iter_references(r):
                target = index.resolve(ref, rec.bundle_id)
                if target is not None and target != (r["resourceType"], r["id"]) and index.target_rejected(*target):
                    issues.append(Issue("DQ_REFERENCE_TARGET_QUARANTINED", "error", path, "Referenced resource failed another quality check"))
    return issues


def _source_location(rec: Record) -> dict[str, int]:
    """Report exact input position without copying a filename or filesystem path."""
    location = {"file_index": rec.source_file_index, "line": rec.source_line}
    if rec.source_entry is not None:
        location["bundle_entry"] = rec.source_entry
    return location


def validate_ndjson(inputs: list[str | Path], output_dir: str | Path, policy: Policy | None = None) -> Summary:
    """Strict, offline quality gate; output dir MUST NOT exist. Uses local PHI-sensitive temp index."""
    p = policy or Policy()
    files = sorted(Path(x) for x in inputs)
    if not files:
        raise ValueError("At least one input file is required")
    for f in files:
        if not f.is_file():
            raise FileNotFoundError(f)
    destination = Path(output_dir)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    # Outputs are intentionally PHI-sensitive. No implicit cloud calls or telemetry.
    idx_file = destination / ".reference-index.sqlite3"
    summary = Summary()
    try:
        index_fd = os.open(idx_file, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(index_fd)
        connection = sqlite3.connect(idx_file)
        try:
            index = _Index(connection, secrets.token_bytes(32), p.trusted_reference_bases)
            with connection:
                for rec in records_from_ndjson(files):
                    index.add(rec)
            # Precompute validity of *all* indexed resources before writing
            # accepted output. Otherwise an accepted Observation could reference a
            # Patient that exists in the input but is itself quarantined.
            with connection:
                for rec in records_from_ndjson(files):
                    r = rec.resource
                    if isinstance(r, dict):
                        index.plan_dependency(r, rec.bundle_id)
                    if isinstance(r, dict) and isinstance(r.get("resourceType"), str) and isinstance(r.get("id"), str):
                        if any(issue.severity == "error" for issue in _record_issues(rec, index, p)):
                            if TYPE_RE.fullmatch(r["resourceType"]) and ID_RE.fullmatch(r["id"]):
                                index.mark_rejected(r["resourceType"], r["id"])
                index.close_rejections()
            with contextlib.ExitStack() as stack:
                accepted_dir = destination / "accepted"
                accepted_dir.mkdir(mode=0o700)
                passed_files: dict[str, Any] = {}
                rejected = stack.enter_context(_create_private_file(destination / "quarantine.ndjson"))
                issues_file = stack.enter_context(_create_private_file(destination / "issues.ndjson"))
                outcome_file = stack.enter_context(_create_private_file(destination / "operation-outcomes.ndjson"))
                for rec in records_from_ndjson(files):
                    r = rec.resource
                    summary.total += 1
                    typ = r.get("resourceType") if isinstance(r, dict) else None
                    if isinstance(typ, str):
                        summary.by_resource_type[typ] = summary.by_resource_type.get(typ, 0) + 1
                    issues = _record_issues(rec, index, p, propagate=True)
                    invalid = any(i.severity == "error" for i in issues)
                    if invalid:
                        summary.quarantined += 1
                        # Quarantine is a private *envelope*, not FHIR NDJSON.
                        # Source data for malformed JSON is intentionally not echoed.
                        rejected.write(json.dumps({"record_index": rec.index, "source": _source_location(rec), "resource": None if rec.parse_error else r},
                                                  separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n")
                    else:
                        summary.accepted += 1
                        if typ not in passed_files:
                            passed_files[typ] = stack.enter_context(_create_private_file(accepted_dir / (typ + ".ndjson")))
                        passed_files[typ].write(json.dumps(r, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n")
                    if issues:
                        for issue in issues:
                            summary.by_issue_code[issue.code] = summary.by_issue_code.get(issue.code, 0) + 1
                            if issue.severity == "warning":
                                summary.warnings += 1
                        issues_file.write(json.dumps({"record_index": rec.index, "source": _source_location(rec),
                                                      "issues": [issue.as_dict() for issue in issues]}, separators=(",", ":")) + "\n")
                        outcome_file.write(json.dumps({"record_index": rec.index, "source": _source_location(rec), "outcome": operation_outcome(issues)}, separators=(",", ":")) + "\n")
                with _create_private_file(destination / "summary.json") as report:
                    report.write(json.dumps({**summary.as_dict(), "readiness": assess_readiness(summary)}, indent=2, sort_keys=True) + "\n")
        finally:
            connection.close()
    finally:
        # Includes SQLite cleanup on exceptions; output files deliberately kept for investigation.
        idx_file.unlink(missing_ok=True)
    return summary
