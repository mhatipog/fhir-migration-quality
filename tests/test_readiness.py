from fhir_quality.models import Issue, Summary
from fhir_quality.readiness import ReadinessThresholds, assess_readiness
from fhir_quality.outcome import operation_outcome
import pytest


def test_readiness_ready():
    summary = Summary(total=100, accepted=100, by_resource_type={"Patient": 50, "Observation": 50})
    report = assess_readiness(summary, ReadinessThresholds(required_resource_types=("Patient", "Observation")))
    assert report["ready"]


def test_readiness_blocked_by_conflict_even_if_high_passrate():
    summary = Summary(total=1000, accepted=999, quarantined=1, by_issue_code={"DQ_DUPLICATE_PATIENT_IDENTIFIER": 1})
    r = assess_readiness(summary)
    assert not r["ready"]
    assert any(c["name"] == "duplicate_patient_identifiers" and not c["passed"] for c in r["checks"])


def test_empty_dataset_not_ready():
    assert not assess_readiness(Summary())["ready"]


def test_invalid_policy():
    with pytest.raises(ValueError):
        ReadinessThresholds(min_pass_rate=5)


def test_operation_outcome_no_identity_leak():
    issue = Issue("DQ_REFERENCE_NOT_FOUND", "error", "/subject/reference", "Referenced resource not found in local dataset")
    obj = operation_outcome([issue])
    assert obj["resourceType"] == "OperationOutcome"
    assert obj["issue"][0]["code"] == "not-found"
    assert "Patient/p" not in str(obj)
    with pytest.raises(ValueError):
        operation_outcome([])
