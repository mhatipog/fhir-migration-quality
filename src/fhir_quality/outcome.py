"""FHIR R4 OperationOutcome summaries with no source values or identifiers."""
from __future__ import annotations
from .models import Issue


def operation_outcome(issues: list[Issue]) -> dict:
    """Represent the local validation issues as an R4 OperationOutcome resource.

    Intentionally omit source identifiers and raw payload. Output remains sensitive
    in context: file order and issue patterns may still be linkable.
    """
    if not issues:
        raise ValueError("OperationOutcome requires at least one issue")
    codes = {
        "FHIR_JSON_INVALID": "structure", "FHIR_OBJECT_REQUIRED": "structure",
        "FHIR_RESOURCE_TYPE": "structure", "DQ_ID_REQUIRED": "required",
        "DQ_DUPLICATE_LOGICAL_ID": "duplicate", "DQ_DUPLICATE_PATIENT_IDENTIFIER": "duplicate",
        "DQ_REFERENCE_NOT_FOUND": "not-found", "DQ_CONTAINED_REFERENCE_MISSING": "not-found",
        "DQ_PATIENT_REFERENCE_NOT_FOUND": "not-found", "DQ_ENCOUNTER_REFERENCE_NOT_FOUND": "not-found",
    }
    return {"resourceType": "OperationOutcome", "issue": [
        {"severity": i.severity, "code": codes.get(i.code, "business-rule"),
         "details": {"coding": [{"system": "urn:fhir-migration-quality:issues", "code": i.code}],
                     "text": i.message}}
        for i in issues
    ]}
