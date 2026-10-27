"""Pre-migration profiling of legacy CSVs, without retaining identifiers in reports.

The local index holds HMACs of source keys, never raw source IDs. This does not
remove the need for secure storage and medical-data access controls.
"""
from __future__ import annotations
import csv
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from .rules import _valid_date

REQUIRED = {
    "patients": ("patient_id",),
    "encounters": ("encounter_id", "patient_id", "start"),
    "observations": ("observation_id", "patient_id", "code_system", "code", "effective_at", "value"),
}
KEYS = {"patients": "patient_id", "encounters": "encounter_id", "observations": "observation_id"}


def _schema_row(row: dict[str, Any], kind: str, column_map: dict[str, dict[str, str]]) -> dict[str, Any]:
    result = dict(row)
    for canonical, original in column_map.get(kind, {}).items():
        result[canonical] = row.get(original)
    return result


def _rows(path: Path, kind: str, column_map: dict[str, dict[str, str]]):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            yield _schema_row(row, kind, column_map)


def profile_legacy(input_dir: str | Path, *, column_map: dict[str, dict[str, str]] | None = None) -> dict:
    """Preflight joins, missing keys, duplicates, drift and date formats.

    Missing source tables are reported as not-present rather than silently
    assumed. Completeness checks are policy flags, not HL7 requirements.
    """
    root = Path(input_dir)
    mapping = column_map or {}
    if not isinstance(mapping, dict) or any(k not in REQUIRED or not isinstance(v, dict) or
                                            any(not isinstance(a, str) or not isinstance(b, str) or not a or not b for a, b in v.items())
                                            for k, v in mapping.items()):
        raise ValueError("Invalid column mapping")
    present = [kind for kind in REQUIRED if (root / (kind + ".csv")).is_file()]
    if not present:
        raise FileNotFoundError("No supported legacy CSV files found")
    counts = {kind: 0 for kind in REQUIRED}
    issues: Counter[str] = Counter()
    missing_cols: dict[str, list[str]] = {}
    salt = secrets.token_bytes(32)

    def digest(kind: str, source: Any) -> str | None:
        if not isinstance(source, str) or not source.strip():
            return None
        data = json.dumps([kind, source.strip()], ensure_ascii=False, separators=(",", ":")).encode()
        return hmac.new(salt, data, hashlib.sha256).hexdigest()

    with tempfile.TemporaryDirectory(prefix="fhir-legacy-index-") as temp_dir:
        idx_path = Path(temp_dir) / "index.sqlite3"
        fd = os.open(idx_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        db = sqlite3.connect(idx_path)
        try:
            db.executescript("""
                CREATE TABLE keys (kind TEXT, keyhash TEXT, patienthash TEXT);
                CREATE INDEX keys_kindhash ON keys(kind,keyhash);
            """)
            for kind in present:
                with (root / (kind + ".csv")).open("r", encoding="utf-8-sig", newline="") as f:
                    header = (csv.DictReader(f).fieldnames or [])
                missing = [c for c in REQUIRED[kind] if mapping.get(kind, {}).get(c, c) not in header]
                if missing:
                    missing_cols[kind] = missing
                    issues["SOURCE_SCHEMA_MISSING_COLUMN"] += len(missing)
                with db:
                    for row in _rows(root / (kind + ".csv"), kind, mapping):
                        counts[kind] += 1
                        keyhash = digest(kind, row.get(KEYS[kind]))
                        patienthash = digest("patients", row.get("patient_id")) if kind != "patients" else None
                        if keyhash is None:
                            issues["SOURCE_KEY_MISSING"] += 1
                        else:
                            db.execute("INSERT INTO keys VALUES (?, ?, ?)", (kind, keyhash, patienthash))
                        if kind == "patients":
                            dob = row.get("birth_date")
                            if dob and not _valid_date(dob, date_time=False):
                                issues["SOURCE_BIRTHDATE_INVALID"] += 1
                            if not dob:
                                issues["SOURCE_BIRTHDATE_ABSENT"] += 1
                        if kind == "encounters":
                            start = row.get("start")
                            if not start or not _valid_date(start, date_time=True):
                                issues["SOURCE_ENCOUNTER_DATE_INVALID"] += 1
                        if kind == "observations":
                            effective = row.get("effective_at")
                            if not effective or not _valid_date(effective, date_time=True):
                                issues["SOURCE_OBSERVATION_DATE_INVALID"] += 1
                            if not row.get("code_system") or not row.get("code"):
                                issues["SOURCE_OBSERVATION_CODE_MISSING"] += 1
                            if row.get("value") in (None, ""):
                                issues["SOURCE_OBSERVATION_VALUE_MISSING"] += 1
            # Count *all* rows involved in duplicate keys, not just second arrivals.
            row = db.execute("SELECT COALESCE(SUM(n),0) FROM (SELECT COUNT(*) AS n FROM keys GROUP BY kind,keyhash HAVING COUNT(*)>1)").fetchone()
            issues["SOURCE_DUPLICATE_KEY_ROWS"] = int(row[0])
            if "patients" not in present and ("encounters" in present or "observations" in present):
                issues["SOURCE_PATIENT_DATASET_ABSENT"] += 1
            if "encounters" not in present and "observations" in present:
                for row in _rows(root / "observations.csv", "observations", mapping):
                    if row.get("encounter_id"):
                        issues["SOURCE_ENCOUNTER_DATASET_ABSENT"] += 1
                        break
            if "patients" in present:
                for kind in ("encounters", "observations"):
                    if kind not in present:
                        continue
                    row = db.execute("""SELECT COUNT(*) FROM keys k LEFT JOIN keys p
                        ON p.kind='patients' AND p.keyhash=k.patienthash
                        WHERE k.kind=? AND k.patienthash IS NOT NULL AND p.keyhash IS NULL""", (kind,)).fetchone()
                    issues["SOURCE_" + kind.upper() + "_PATIENT_MISSING"] = int(row[0])
            if "encounters" in present and "observations" in present:
                # 2nd scan only of observations: raw patient/encounter keys are never in the index.
                for row in _rows(root / "observations.csv", "observations", mapping):
                    enc_id = digest("encounters", row.get("encounter_id"))
                    if not enc_id:
                        continue  # Optional encounter link.
                    candidates = db.execute("SELECT patienthash FROM keys WHERE kind='encounters' AND keyhash=?", (enc_id,)).fetchall()
                    if not candidates:
                        issues["SOURCE_OBSERVATION_ENCOUNTER_MISSING"] += 1
                    elif len(candidates) == 1 and candidates[0][0] != digest("patients", row.get("patient_id")):
                        issues["SOURCE_OBSERVATION_ENCOUNTER_PATIENT_MISMATCH"] += 1
        finally:
            db.close()
    nonblocking = {"SOURCE_BIRTHDATE_ABSENT"}
    violations = {name: num for name, num in issues.items() if num and name not in nonblocking}
    return {"rows": counts, "present_tables": present, "missing_required_columns": missing_cols,
            "issue_counts": dict(sorted((k, v) for k, v in issues.items() if v)),
            "preflight_ready": not violations,
            "caveat": "Source completeness/consistency only; not full FHIR or clinical validation"}
