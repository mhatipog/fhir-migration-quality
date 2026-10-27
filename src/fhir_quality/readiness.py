"""Explainable, data-quality-only migration release gate."""
from __future__ import annotations
from dataclasses import dataclass
from .models import Summary


@dataclass(frozen=True)
class ReadinessThresholds:
    min_pass_rate: float = 0.99
    max_missing_references: int = 0
    max_duplicate_ids: int = 0
    max_patient_mismatches: int = 0
    max_duplicate_patient_identifiers: int = 0
    max_quarantined_reference_targets: int = 0
    max_unverified_versioned_references: int = 0
    required_resource_types: tuple[str, ...] = ()

    def __post_init__(self):
        if not (0 <= self.min_pass_rate <= 1):
            raise ValueError("min_pass_rate must be between 0 and 1")
        if any(getattr(self, field) < 0 for field in ("max_missing_references", "max_duplicate_ids", "max_patient_mismatches", "max_duplicate_patient_identifiers", "max_quarantined_reference_targets", "max_unverified_versioned_references")):
            raise ValueError("Maximum issue counts must be nonnegative")


def assess_readiness(summary: Summary, policy: ReadinessThresholds | None = None) -> dict:
    """Gate observed dataset only. Passing is NOT full FHIR/clinical compliance."""
    p = policy or ReadinessThresholds()
    checks = []

    def check(name: str, observed: int | float, limit: int | float, comparator: str):
        ok = observed >= limit if comparator == ">=" else observed <= limit
        checks.append({"name": name, "observed": observed, "limit": limit, "passed": ok})

    check("nonempty_input", summary.total, 1, ">=")
    check("resource_pass_rate", round(summary.pass_rate, 6), p.min_pass_rate, ">=")
    codes = summary.by_issue_code
    check("missing_references", codes.get("DQ_REFERENCE_NOT_FOUND", 0) + codes.get("DQ_CONTAINED_REFERENCE_MISSING", 0), p.max_missing_references, "<=")
    check("duplicate_logical_ids", codes.get("DQ_DUPLICATE_LOGICAL_ID", 0), p.max_duplicate_ids, "<=")
    check("duplicate_patient_identifiers", codes.get("DQ_DUPLICATE_PATIENT_IDENTIFIER", 0), p.max_duplicate_patient_identifiers, "<=")
    check("patient_encounter_mismatches", codes.get("DQ_PATIENT_ENCOUNTER_MISMATCH", 0), p.max_patient_mismatches, "<=")
    check("quarantined_reference_targets", codes.get("DQ_REFERENCE_TARGET_QUARANTINED", 0), p.max_quarantined_reference_targets, "<=")
    check("unverified_versioned_references", codes.get("DQ_VERSION_REFERENCE_NOT_CHECKED", 0),
          p.max_unverified_versioned_references, "<=")
    for rtype in p.required_resource_types:
        check("required_type:" + rtype, summary.by_resource_type.get(rtype, 0), 1, ">=")
    return {"ready": all(i["passed"] for i in checks), "checks": checks,
            "caveat": "Quality gate only; official profile and terminology validation is separate"}
