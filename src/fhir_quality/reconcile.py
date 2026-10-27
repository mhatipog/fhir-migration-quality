"""Privacy-preserving, bounded-memory comparison of two FHIR R4 snapshots.

This module produces *counts*, not deletion commands or identifying diff rows.
A missing resource in an incomplete export must not be treated as a deletion.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
from typing import Any

from .bulk import records_from_ndjson
from .rules import ID_RE, TYPE_RE


def _canonical_bytes(resource: dict[str, Any], ignore_meta_last_updated: bool) -> bytes:
    # meta.lastUpdated is transport metadata. Excluding it is an explicit opt-in:
    # changes to other meta fields remain meaningful.
    if ignore_meta_last_updated and isinstance(resource.get("meta"), dict):
        resource = dict(resource)
        meta = dict(resource["meta"])
        meta.pop("lastUpdated", None)
        resource["meta"] = meta
    return json.dumps(resource, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def compare_snapshots(
    previous: list[str | Path], current: list[str | Path], *,
    secret: bytes, ignore_meta_last_updated: bool = False,
    complete_snapshots: bool = False,
) -> dict:
    """Return a non-identifying change summary using an on-disk SQLite index.

    `complete_snapshots=True` means both inputs contain the complete same scope.
    Without this, disappearing keys are reported as *unobserved*, not deleted.
    Key and content HMACs prevent trivial brute-force recovery of predictable IDs.
    The temp SQLite file still contains sensitive correlation material.
    """
    if len(secret) < 16:
        raise ValueError("Reconciliation secret must contain at least 16 bytes")
    if not previous or not current:
        raise ValueError("Both snapshots require at least one input file")
    for file in (*previous, *current):
        if not Path(file).is_file():
            raise FileNotFoundError("Snapshot file does not exist")
    # Separate HMAC domains so tokens for identifiers are not content fingerprints.
    def token(domain: bytes, *parts: str | bytes) -> bytes:
        message = b"\0".join((x.encode("utf-8") if isinstance(x, str) else x) for x in parts)
        return hmac.new(secret, domain + b"\0" + message, hashlib.sha256).digest()

    with tempfile.TemporaryDirectory(prefix="fhir-snapshot-") as scratch:
        tmp = Path(scratch)
        os.chmod(tmp, 0o700)
        path = tmp / "snapshot.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE snapshot (side INTEGER, kind TEXT, key BLOB, digest BLOB, PRIMARY KEY (side,kind,key)) WITHOUT ROWID")
            totals = [0, 0]
            invalid = [0, 0]
            duplicate = [0, 0]
            # Inserting explicitly catches duplicates; never silently pick a winner.
            for side, paths in enumerate((previous, current)):
                with db:
                    for rec in records_from_ndjson([Path(x) for x in paths]):
                        totals[side] += 1
                        resource = rec.resource
                        if not isinstance(resource, dict):
                            invalid[side] += 1
                            continue
                        kind, rid = resource.get("resourceType"), resource.get("id")
                        if not isinstance(kind, str) or not TYPE_RE.fullmatch(kind) or not isinstance(rid, str) or not ID_RE.fullmatch(rid):
                            invalid[side] += 1
                            continue
                        try:
                            digest = token(b"content", _canonical_bytes(resource, ignore_meta_last_updated))
                        except (ValueError, TypeError, RecursionError):
                            invalid[side] += 1
                            continue
                        key = token(b"logical-id", kind, rid)
                        try:
                            db.execute("INSERT INTO snapshot VALUES (?,?,?,?)", (side, kind, key, digest))
                        except sqlite3.IntegrityError:
                            duplicate[side] += 1
            rows = db.execute("""
                SELECT c.kind,
                       SUM(CASE WHEN p.key IS NULL THEN 1 ELSE 0 END) AS added,
                       SUM(CASE WHEN p.key IS NOT NULL AND p.digest != c.digest THEN 1 ELSE 0 END) AS modified,
                       SUM(CASE WHEN p.key IS NOT NULL AND p.digest = c.digest THEN 1 ELSE 0 END) AS unchanged
                FROM snapshot c LEFT JOIN snapshot p ON p.side=0 AND p.kind=c.kind AND p.key=c.key
                WHERE c.side=1 GROUP BY c.kind ORDER BY c.kind
            """).fetchall()
            missing = dict(db.execute("""
                SELECT p.kind, COUNT(*) FROM snapshot p
                LEFT JOIN snapshot c ON c.side=1 AND c.kind=p.kind AND c.key=p.key
                WHERE p.side=0 AND c.key IS NULL GROUP BY p.kind
            """).fetchall())
            by_type = {}
            for kind, added, modified, unchanged in rows:
                by_type[kind] = {"added": added, "modified": modified, "unchanged": unchanged,
                                 "deleted" if complete_snapshots else "unobserved": missing.pop(kind, 0)}
            for kind, count in missing.items():
                by_type[kind] = {"added": 0, "modified": 0, "unchanged": 0,
                                 "deleted" if complete_snapshots else "unobserved": count}
            by_type = dict(sorted(by_type.items()))
            safe = invalid == [0, 0] and duplicate == [0, 0]
            key = "deleted" if complete_snapshots else "unobserved"
            sums = {field: sum(stats[field] for stats in by_type.values())
                    for field in ("added", "modified", "unchanged", key)}
            return {
                "previous_records": totals[0], "current_records": totals[1],
                "invalid_records": {"previous": invalid[0], "current": invalid[1]},
                "duplicate_keys": {"previous": duplicate[0], "current": duplicate[1]},
                "complete_snapshots": complete_snapshots,
                "safe_for_change_accounting": safe,
                "totals": sums,
                "by_resource_type": by_type,
                "notes": (
                    "Deletion candidates require explicit confirmation of identical, complete extraction scope. "
                    "This report never performs deletes."
                    if complete_snapshots else
                    "Disappeared records are unobserved, not deletions, because extraction completeness was not asserted."
                ),
            }
        finally:
            db.close()
