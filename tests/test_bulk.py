import json
import stat
from pathlib import Path
from fhir_quality.bulk import validate_ndjson
from fhir_quality.rules import Policy


def write(path, rows):
    path.write_text("\n".join(json.dumps(r) if not isinstance(r, str) else r for r in rows) + "\n")


def read_jsonl(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x]


def test_reconciliation_and_security(tmp_path):
    data = tmp_path / "in.ndjson"
    write(data, [
        {"resourceType": "Patient", "id": "p", "birthDate": "2000-01-01"},
        {"resourceType": "Encounter", "id": "e", "status": "finished", "class": {"system": "s", "code": "c"}, "subject": {"reference": "Patient/p"}},
        {"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "x"},
         "subject": {"reference": "Patient/p"}, "encounter": {"reference": "Encounter/e"}, "effectiveDateTime": "2024-09-01"},
        {"resourceType": "Observation", "id": "bad", "status": "final", "code": {"text": "x"}, "subject": {"reference": "Patient/unknown"}},
    ])
    result = validate_ndjson([data], tmp_path / "result")
    assert (result.total, result.accepted, result.quarantined) == (4, 3, 1)
    assert sum(result.by_issue_code.values()) == 1
    assert result.by_issue_code["DQ_REFERENCE_NOT_FOUND"] == 1
    assert len(read_jsonl(tmp_path / "result" / "quarantine.ndjson")) == 1
    assert sorted(x.name for x in (tmp_path / "result" / "accepted").iterdir()) == ["Encounter.ndjson", "Observation.ndjson", "Patient.ndjson"]
    assert sum(len(read_jsonl(x)) for x in (tmp_path / "result" / "accepted").iterdir()) == 3
    assert not (tmp_path / "result" / ".reference-index.sqlite3").exists()
    if __import__("os").name == "posix":
        assert stat.S_IMODE((tmp_path / "result").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "result" / "quarantine.ndjson").stat().st_mode) == 0o600


def test_duplicate_quarantines_both(tmp_path):
    src = tmp_path / "source.ndjson"
    write(src, [{"resourceType": "Patient", "id": "x"}, {"resourceType": "Patient", "id": "x"}])
    s = validate_ndjson([src], tmp_path / "out")
    assert s.quarantined == 2
    assert s.by_issue_code["DQ_DUPLICATE_LOGICAL_ID"] == 2


def test_duplicate_patient_identifiers(tmp_path):
    src = tmp_path / "source.ndjson"
    write(src, [{"resourceType": "Patient", "id": "x", "identifier": [{"system": "s", "value": "secret-id"}]},
                {"resourceType": "Patient", "id": "y", "identifier": [{"system": "s", "value": "secret-id"}]}])
    s = validate_ndjson([src], tmp_path / "out")
    assert s.quarantined == 2
    assert s.by_issue_code["DQ_DUPLICATE_PATIENT_IDENTIFIER"] == 2
    assert "secret-id" not in (tmp_path / "out" / "issues.ndjson").read_text()


def test_patient_encounter_mismatch(tmp_path):
    src = tmp_path / "source.ndjson"
    write(src, [{"resourceType": "Patient", "id": "p1"}, {"resourceType": "Patient", "id": "p2"},
                {"resourceType": "Encounter", "id": "e", "status": "finished", "class": {"system": "x", "code": "c"}, "subject": {"reference": "Patient/p1"}},
                {"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "glucose"},
                 "subject": {"reference": "Patient/p2"}, "encounter": {"reference": "Encounter/e"}}])
    s = validate_ndjson([src], tmp_path / "out")
    assert s.quarantined == 1
    assert "DQ_PATIENT_ENCOUNTER_MISMATCH" in s.by_issue_code


def test_event_before_birth(tmp_path):
    src = tmp_path / "input.ndjson"
    write(src, [{"resourceType": "Patient", "id": "p", "birthDate": "2020-01-01"},
                {"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "something"}, "subject": {"reference": "Patient/p"}, "effectiveDateTime": "2019-12-31"}])
    s = validate_ndjson([src], tmp_path / "out")
    assert s.by_issue_code["DQ_EVENT_BEFORE_BIRTH"] == 1


def test_bundle_fullurl_urn_and_contained(tmp_path):
    src = tmp_path / "input.ndjson"
    write(src, [{"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": "urn:uuid:abc", "resource": {"resourceType": "Patient", "id": "p"}},
        {"fullUrl": "urn:uuid:def", "resource": {"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "x"}, "subject": {"reference": "urn:uuid:abc"}}}
    ]}])
    s = validate_ndjson([src], tmp_path / "out")
    assert s.accepted == 2
    assert not s.by_issue_code


def test_bad_bundle_alias_duplicate(tmp_path):
    src = tmp_path / "input.ndjson"
    write(src, [{"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": "urn:uuid:abc", "resource": {"resourceType": "Patient", "id": "p1"}},
        {"fullUrl": "urn:uuid:abc", "resource": {"resourceType": "Patient", "id": "p2"}},
        {"resource": {"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "x"}, "subject": {"reference": "urn:uuid:abc"}}}
    ]}])
    s = validate_ndjson([src], tmp_path / "out")
    assert s.by_issue_code["DQ_AMBIGUOUS_FULLURL"] == 1


def test_open_world_reference_warns(tmp_path):
    src = tmp_path / "input.ndjson"
    write(src, [{"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "x"},
                 "subject": {"reference": "Patient/missing"}}])
    s = validate_ndjson([src], tmp_path / "out", Policy(reference_scope="open"))
    assert s.accepted == 1 and s.warnings == 1


def test_external_reference_warning(tmp_path):
    src = tmp_path / "input.ndjson"
    write(src, [{"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "x"},
                 "subject": {"reference": "https://example.org/fhir/Patient/1"}}])
    s = validate_ndjson([src], tmp_path / "out")
    assert s.accepted == 1 and s.warnings == 1
    assert s.by_issue_code["DQ_REFERENCE_NOT_CHECKED"] == 1


def test_malformed_json_duplicate_object_keys_no_leak(tmp_path):
    src = tmp_path / "input.ndjson"
    src.write_text('{"resourceType":"Patient","id":"a","id":"sensitive"}\nnot-json secret info\n')
    s = validate_ndjson([src], tmp_path / "out")
    assert s.quarantined == 2
    assert all(x["resource"] is None for x in read_jsonl(tmp_path / "out" / "quarantine.ndjson"))
    assert "sensitive" not in (tmp_path / "out" / "issues.ndjson").read_text()


def test_gzip_input(tmp_path):
    import gzip
    src = tmp_path / "data.ndjson.gz"
    with gzip.open(src, "wt") as f:
        f.write('{"resourceType":"Patient","id":"p"}\n')
    assert validate_ndjson([src], tmp_path / "out").accepted == 1


def test_already_existing_destination_refused(tmp_path):
    import pytest
    src = tmp_path / "in.ndjson"
    write(src, [{"resourceType": "Patient", "id": "p"}])
    (tmp_path / "out").mkdir()
    with pytest.raises(FileExistsError):
        validate_ndjson([src], tmp_path / "out")


def test_urn_aliases_scoped_to_separate_bundles(tmp_path):
    source = tmp_path / "input.ndjson"
    write(source, [
        {"resourceType": "Bundle", "type": "collection", "entry": [
            {"fullUrl": "urn:uuid:shared", "resource": {"resourceType": "Patient", "id": "pa"}},
            {"resource": {"resourceType": "Observation", "id": "oa", "status": "final", "code": {"text": "test"}, "subject": {"reference": "urn:uuid:shared"}}}]},
        {"resourceType": "Bundle", "type": "collection", "entry": [
            {"fullUrl": "urn:uuid:shared", "resource": {"resourceType": "Patient", "id": "pb"}},
            {"resource": {"resourceType": "Observation", "id": "ob", "status": "final", "code": {"text": "test"}, "subject": {"reference": "urn:uuid:shared"}}}]},
    ])
    s = validate_ndjson([source], tmp_path / "out")
    assert s.accepted == 4
    assert s.quarantined == 0


def test_large_line_is_quarantined_without_retention(tmp_path):
    from fhir_quality.bulk import MAX_LINE_CHARS
    src = tmp_path / "input.ndjson"
    src.write_text("x" * (MAX_LINE_CHARS + 10) + "\n" + '{"resourceType":"Patient","id":"p"}\n')
    s = validate_ndjson([src], tmp_path / "out")
    assert s.total == 2 and s.accepted == 1 and s.quarantined == 1
    assert "x" * 50 not in (tmp_path / "out" / "issues.ndjson").read_text()
