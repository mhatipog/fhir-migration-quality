import json
from fhir_quality.rules import Policy, local_issues
from fhir_quality.bulk import validate_ndjson


def codes(resource):
    return {x.code for x in local_issues(resource)}


def test_wrong_type_status_does_not_raise():
    for status in ({"bad":1}, [], 7, None):
        assert "FHIR_OBSERVATION_STATUS" in codes({"resourceType":"Observation","id":"o","status":status,"code":{"text":"x"}})
        assert "FHIR_ENCOUNTER_STATUS" in codes({"resourceType":"Encounter","id":"e","status":status,"class":{"system":"s","code":"c"}})


def test_exact_intraday_chronology_and_offsets():
    enc = {"resourceType":"Encounter","id":"e","status":"finished","class":{"system":"x","code":"x"},
           "period":{"start":"2026-10-27T12:00:00+02:00", "end":"2026-10-27T09:00:00Z"}}
    assert "DQ_PERIOD_ORDER" in codes(enc)
    enc["period"]["end"] = "2026-10-27T11:00:00Z"
    assert "DQ_PERIOD_ORDER" not in codes(enc)


def test_observation_choices_and_instant():
    obs = {"resourceType":"Observation","id":"o","status":"final","code":{"text":"Hb"},
           "valueString":"low", "valueQuantity":{"value":1,"system":"http://unitsofmeasure.org"},
           "effectiveDateTime":"2026-01-01T00:00:00Z", "effectivePeriod":{"start":"2025-01-01"},
           "dataAbsentReason":{"text":"unknown"},"issued":"2026-01-01"}
    assert {"FHIR_OBSERVATION_MULTIPLE_VALUES","FHIR_OBSERVATION_MULTIPLE_EFFECTIVES",
            "FHIR_OBSERVATION_VALUE_WITH_ABSENT_REASON","FHIR_OBSERVATION_ISSUED","DQ_UCUM_CODE_MISSING"} <= codes(obs)
    assert "FHIR_OBSERVATION_ISSUED" not in codes({**obs,"issued":"2026-01-01T10:00:00+01:00"})


def test_corrupt_field_in_batch_is_quarantined(tmp_path):
    file = tmp_path / "observations.ndjson"
    row={"resourceType":"Observation","id":"bad","status":{"oops": True},"code":{"text":"x"}}
    file.write_text(json.dumps(row)+"\n")
    result=validate_ndjson([file],tmp_path/"out")
    assert result.quarantined==1
    assert result.by_issue_code["FHIR_OBSERVATION_STATUS"]==1


def test_decimal_precision_is_never_silently_rewritten(tmp_path):
    from fhir_quality.rules import strict_json_loads
    import pytest
    for unsafe in ['1.234567890123456789', '1e999', '1e-999']:
        with pytest.raises(ValueError):
            strict_json_loads('{"valueQuantity":{"value":'+unsafe+'}}')
    assert strict_json_loads('{"valueQuantity":{"value":1.2500}}')['valueQuantity']['value']==1.25
    path=tmp_path/'obs.ndjson'
    path.write_text('{"resourceType":"Observation","id":"x","valueQuantity":{"value":1.234567890123456789}}\n')
    result=validate_ndjson([path],tmp_path/'result')
    assert result.quarantined==1
    assert result.accepted==0


def test_excessive_json_nesting_becomes_quarantine_not_pipeline_crash(tmp_path):
    from fhir_quality.bulk import validate_ndjson
    source = tmp_path / "deep.ndjson"
    deep = '[' * 12000 + '1' + ']' * 12000
    source.write_text(deep + '\n' + '{"resourceType":"Patient","id":"fine"}' + '\n')
    summary = validate_ndjson([source], tmp_path / "out")
    assert (summary.accepted, summary.quarantined) == (1, 1)
    assert summary.by_issue_code["FHIR_JSON_INVALID"] == 1
