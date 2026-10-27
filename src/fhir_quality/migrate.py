"""Deterministic legacy patient / encounter / observation CSV -> FHIR R4 NDJSON.

Inputs and mapped outputs contain PHI. Use private local storage only.
"""
from __future__ import annotations
import csv
import hashlib
import hmac
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .rules import _valid_date

_KINDS = ("patients", "encounters", "observations")
_DEFAULT_CLASS = {"ambulatory": "AMB", "inpatient": "IMP", "emergency": "EMER", "virtual": "VR"}
_GENDERS = {"male", "female", "other", "unknown"}


class MigrationError(ValueError):
    """Fixed non-sensitive message, safe to put in a report."""


@dataclass(frozen=True)
class MigrationConfig:
    secret: bytes
    namespace: str = "local-demo"
    identifier_system: str | None = None

    def __post_init__(self):
        if len(self.secret) < 16:
            raise ValueError("Migration key must be at least 16 bytes")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.namespace):
            raise ValueError("Invalid namespace")

    @classmethod
    def from_env(cls, *, namespace: str = "local-demo") -> MigrationConfig:
        value = os.environ.get("FHIR_MIGRATION_KEY")
        if not value or len(value.encode()) < 16:
            raise ValueError("Set FHIR_MIGRATION_KEY to a stable secret of at least 16 bytes")
        return cls(value.encode(), namespace)


def stable_id(config: MigrationConfig, kind: str, legacy_id: str) -> str:
    if not isinstance(legacy_id, str) or not legacy_id.strip():
        raise MigrationError("Missing legacy source key")
    if kind not in {"Patient", "Encounter", "Observation"}:
        raise ValueError("Unsupported resource mapping kind")
    # HMAC: unlike bare SHA256, discourages recovery of predictable patient IDs.
    digest = hmac.new(config.secret, json.dumps([config.namespace, kind, legacy_id], separators=(",", ":")).encode(), hashlib.sha256).hexdigest()[:40]
    return {"Patient": "pat", "Encounter": "enc", "Observation": "obs"}[kind] + "-" + digest


def _needed(row: dict, field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MigrationError("Missing required source field: " + field)
    return value.strip()


def _date(row: dict, key: str, required: bool = False) -> str | None:
    value = row.get(key, "")
    if not value:
        if required:
            raise MigrationError("Missing required source date: " + key)
        return None
    if not _valid_date(value, date_time=("T" in value)):
        raise MigrationError("Invalid ISO date/dateTime: " + key)
    return value


def map_patient(row: dict, config: MigrationConfig) -> dict:
    pk = _needed(row, "patient_id")
    patient: dict[str, Any] = {"resourceType": "Patient", "id": stable_id(config, "Patient", pk)}
    dob = _date(row, "birth_date")
    if dob:
        if "T" in dob:
            raise MigrationError("Patient birth_date must be a date, not dateTime")
        patient["birthDate"] = dob
    gender = (row.get("gender") or "").strip().lower()
    if gender:
        if gender not in _GENDERS:
            raise MigrationError("Unknown FHIR administrative gender")
        patient["gender"] = gender
    if config.identifier_system:
        # Explicit opt-in only; source keys may be direct identifiers.
        patient["identifier"] = [{"system": config.identifier_system, "value": pk}]
    return patient


def map_encounter(row: dict, config: MigrationConfig) -> dict:
    eid = _needed(row, "encounter_id")
    pid = _needed(row, "patient_id")
    status = (row.get("status") or "finished").strip() or "finished"
    klass = (row.get("class", "ambulatory") or "ambulatory").lower()
    if status not in {"planned", "arrived", "triaged", "in-progress", "onleave", "finished", "cancelled", "entered-in-error", "unknown"}:
        raise MigrationError("Invalid R4 encounter status")
    if klass not in _DEFAULT_CLASS:
        raise MigrationError("Unsupported encounter class mapping")
    period_start = _date(row, "start", required=True)
    period_end = _date(row, "end")
    enc: dict[str, Any] = {
        "resourceType": "Encounter", "id": stable_id(config, "Encounter", eid),
        "status": status,
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": _DEFAULT_CLASS[klass]},
        "subject": {"reference": "Patient/" + stable_id(config, "Patient", pid)},
        "period": {"start": period_start},
    }
    if period_end:
        enc["period"]["end"] = period_end
    return enc


def map_observation(row: dict, config: MigrationConfig) -> dict:
    oid = _needed(row, "observation_id")
    pid = _needed(row, "patient_id")
    code = _needed(row, "code")
    system = _needed(row, "code_system")
    timestamp = _date(row, "effective_at", required=True)
    status = (row.get("status") or "final").strip() or "final"
    if status not in {"registered", "preliminary", "final", "amended", "corrected", "cancelled", "entered-in-error", "unknown"}:
        raise MigrationError("Invalid R4 observation status")
    observation: dict[str, Any] = {
        "resourceType": "Observation", "id": stable_id(config, "Observation", oid),
        "status": status, "code": {"coding": [{"system": system, "code": code}]},
        "subject": {"reference": "Patient/" + stable_id(config, "Patient", pid)},
        "effectiveDateTime": timestamp,
    }
    if row.get("display"):
        observation["code"]["coding"][0]["display"] = row["display"]
    if row.get("encounter_id"):
        observation["encounter"] = {"reference": "Encounter/" + stable_id(config, "Encounter", row["encounter_id"].strip())}
    kind = (row.get("value_type") or "quantity").strip().lower()
    value = _needed(row, "value")
    if kind == "quantity":
        try:
            from decimal import Decimal, InvalidOperation
            exact = Decimal(value)
            if not exact.is_finite() or len(exact.as_tuple().digits) > 15:
                raise ValueError("out-of-range-or-precision")
            numerical = float(exact)
            if not math.isfinite(numerical):
                raise ValueError("out-of-range")
        except (InvalidOperation, ValueError, OverflowError):
            raise MigrationError("Quantity must be finite numeric with at most 15 significant digits") from None
        quantity: dict[str, Any] = {"value": numerical}
        if row.get("unit_code") and row.get("unit_system", "http://unitsofmeasure.org") == "http://unitsofmeasure.org":
            quantity.update({"system": "http://unitsofmeasure.org", "code": row["unit_code"]})
            if row.get("unit_display"):
                quantity["unit"] = row["unit_display"]
        elif row.get("unit_code"):
            raise MigrationError("Only UCUM quantity unit codes supported")
        observation["valueQuantity"] = quantity
    elif kind == "string":
        observation["valueString"] = value
    elif kind == "boolean":
        if value.lower() not in {"true", "false"}:
            raise MigrationError("Boolean must be true or false")
        observation["valueBoolean"] = value.lower() == "true"
    else:
        raise MigrationError("Unsupported observation value_type")
    return observation


def convert_csv(input_dir: str | Path, output_file: str | Path, config: MigrationConfig, *,
                column_map: dict[str, dict[str, str]] | None = None) -> dict:
    """Create FHIR NDJSON, fail per-row safely, and return de-identified counts.

    Rejects malformed rows rather than leaking their raw contents into logs. This step
    catches row-local errors; run validate_ndjson to check cross-file relationships.
    """
    inputs = Path(input_dir)
    outfile = Path(output_file)
    if outfile.exists():
        raise FileExistsError("Output must not already exist")
    if not outfile.parent.exists():
        raise FileNotFoundError("Output parent directory must already exist")
    column_map = column_map or {}
    if any(kind not in _KINDS or not isinstance(mapping, dict) or
           any(not isinstance(k, str) or not isinstance(v, str) or not k or not v for k, v in mapping.items())
           for kind, mapping in column_map.items()):
        raise ValueError("Invalid column mapping")
    if not any((inputs / (kind + ".csv")).is_file() for kind in _KINDS):
        raise FileNotFoundError("No supported legacy CSV files found")
    summary = {"total": 0, "mapped": 0, "rejected": 0, "by_kind": {}, "issues": {}}
    fd = os.open(outfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as dest:
            for kind, mapper in (("patients", map_patient), ("encounters", map_encounter), ("observations", map_observation)):
                file = inputs / (kind + ".csv")
                if not file.exists():
                    continue
                with file.open("r", encoding="utf-8-sig", newline="") as src:
                    reader = csv.DictReader(src)
                    mapping = column_map.get(kind, {})
                    for row in reader:
                        summary["total"] += 1
                        # Explicit rename without eval, expressions, or accidental overwrites.
                        normalized = dict(row)
                        for canonical, source in mapping.items():
                            normalized[canonical] = row.get(source)
                        try:
                            resource = mapper(normalized, config)
                        except MigrationError as exc:
                            summary["rejected"] += 1
                            summary["issues"][str(exc)] = summary["issues"].get(str(exc), 0) + 1
                            continue
                        dest.write(json.dumps(resource, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
                        summary["mapped"] += 1
                        summary["by_kind"][kind] = summary["by_kind"].get(kind, 0) + 1
    except Exception:
        outfile.unlink(missing_ok=True)
        raise
    return summary
