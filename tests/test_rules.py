import json
import pytest
from fhir_quality.rules import (Policy, _valid_date, iter_references, local_issues,
                                relative_key, strict_json_loads, DuplicateJSONKey)

P = {"resourceType": "Patient", "id": "p1", "birthDate": "1990-02-28"}
E = {"resourceType": "Encounter", "id": "e1", "status": "finished",
     "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
     "subject": {"reference": "Patient/p1"}}
O = {"resourceType": "Observation", "id": "o1", "status": "final", "code": {"coding": [{"system": "http://loinc.org", "code": "1234-5"}]},
     "subject": {"reference": "Patient/p1"}, "valueQuantity": {"value": 5.0}}

@pytest.mark.parametrize("obj,code", [
    ({"resourceType": "Patient", "id": "bad id"}, "DQ_ID_REQUIRED"),
    ({"resourceType": "Patient", "id": "p", "birthDate": "2025-02-29"}, "FHIR_PATIENT_BIRTHDATE"),
    ({"resourceType": "Encounter", "id": "e", "status": "bad", "class": {"code": "IMP"}}, "FHIR_ENCOUNTER_STATUS"),
    ({"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "test"}}, "DQ_OBSERVATION_SUBJECT"),
    ({"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "test"}, "subject": {"reference": "Patient/p"}, "valueQuantity": {"value": "123"}}, "FHIR_QUANTITY_VALUE"),
    ([], "FHIR_OBJECT_REQUIRED"),
    ({"id": "abc"}, "FHIR_RESOURCE_TYPE"),
])
def test_bad_local(obj, code):
    assert code in {i.code for i in local_issues(obj)}

@pytest.mark.parametrize("obj", [P, E, O])
def test_basic_valid(obj):
    assert not local_issues(obj)

@pytest.mark.parametrize("d,expected", [
    ("2024", True), ("2024-02", True), ("2024-02-29", True), ("2023-02-29", False),
    ("2024-13", False), ("2024-02-30", False), ("garbage", False),
])
def test_valid_dates(d, expected):
    assert _valid_date(d) == expected

@pytest.mark.parametrize("d,expected", [
    ("2024-01-01T10:00:00Z", True), ("2024-01-01T10:00:00+01:00", True),
    ("2024-01-01T10:00:00", False), ("2024-01-01T25:00:00Z", False),
    ("2024-01-01", True),
])
def test_date_times(d, expected):
    assert _valid_date(d, date_time=True) == expected

@pytest.mark.parametrize("ref,key", [("Patient/p1", ("Patient", "p1")), ("Patient/p1/_history/2", None),
                                     ("Patient?identifier=abc", None), ("https://server/fhir/Patient/p1", None), ("#xyz", None)])
def test_relative_keys(ref, key):
    assert relative_key(ref) == key


def test_duplicate_json_keys():
    with pytest.raises(DuplicateJSONKey):
        strict_json_loads('{"resourceType":"Patient","id":"a","id":"b"}')


def test_nonfinite_json():
    with pytest.raises(ValueError):
        strict_json_loads('{"value":NaN}')


def test_reference_walk_nested():
    src = {"component": [{"valueReference": {"reference": "Patient/p1"}}], "contained": [{"id": "one"}]}
    assert list(iter_references(src)) == [("/component/0/valueReference/reference", "Patient/p1")]


def test_allowlist():
    allowed = Policy(allowed_observation_codes={"http://loinc.org": frozenset({"other"})})
    assert "DQ_CODE_NOT_IN_ALLOWLIST" in {i.code for i in local_issues(O, allowed)}


def test_optional_value_policy():
    base = {k: v for k, v in O.items() if k != "valueQuantity"}
    assert not local_issues(base)
    assert "DQ_OBSERVATION_VALUE" in {i.code for i in local_issues(base, Policy(require_observation_value=True))}


def test_policy_validation():
    with pytest.raises(ValueError):
        Policy(reference_scope="magic")
