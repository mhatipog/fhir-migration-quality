"""Migration extraction completeness and non-identifying record provenance."""
import gzip
import json

import pytest

from fhir_quality.bulk import records_from_ndjson, validate_ndjson
from fhir_quality.rules import Policy


def _dump(path, rows):
    payload = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n"
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(payload)
    else:
        path.write_text(payload)


def test_open_scope_can_differentiate_complete_and_partial_reference_types(tmp_path):
    rows = [
        {"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "demo"},
         "subject": {"reference": "Patient/absent"},
         "performer": [{"reference": "Organization/absent"}]},
    ]
    f = tmp_path / "input.ndjson"
    _dump(f, rows)
    open_result = validate_ndjson([f], tmp_path / "open", Policy(reference_scope="open"))
    assert (open_result.accepted, open_result.quarantined, open_result.warnings) == (1, 0, 2)
    scoped_result = validate_ndjson([f], tmp_path / "scoped", Policy(
        reference_scope="open", complete_resource_types=frozenset({"Patient"})))
    assert (scoped_result.accepted, scoped_result.quarantined, scoped_result.warnings) == (0, 1, 1)
    errors = [r["issues"] for r in map(json.loads, (tmp_path / "scoped" / "issues.ndjson").read_text().splitlines())]
    assert {item["severity"] for group in errors for item in group} == {"warning", "error"}
    assert {item["code"] for group in errors for item in group} == {"DQ_REFERENCE_NOT_FOUND"}


def test_complete_resource_types_only_affects_missing_not_existing(tmp_path):
    f = tmp_path / "source.ndjson"
    _dump(f, [{"resourceType": "Patient", "id": "p"},
              {"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "x"},
               "subject": {"reference": "Patient/p"}, "performer": [{"reference": "Organization/missing"}]}])
    result = validate_ndjson([f], tmp_path / "out", Policy(
        reference_scope="open", complete_resource_types=frozenset({"Patient"})))
    assert result.accepted == 2
    assert result.warnings == 1


def test_lineage_tracks_file_line_and_bundle_entry_without_paths(tmp_path):
    first = tmp_path / "confidential-patient-file.ndjson.gz"
    _dump(first, [{"resourceType": "Patient", "id": "ok"},
                  {"resourceType": "Patient", "id": "bad", "birthDate": "invalid"}])
    second = tmp_path / "second.ndjson"
    _dump(second, [{"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": "urn:uuid:foo", "resource": {"resourceType": "Patient", "id": "good"}},
        {"fullUrl": "urn:uuid:bar", "resource": {"resourceType": "Patient", "id": "invalid", "birthDate": "2026-02-30"}},
        {"fullUrl": "urn:uuid:baz"}
    ]}])
    sources = list(records_from_ndjson([first, second]))
    assert [(r.source_file_index, r.source_line, r.source_entry) for r in sources] == [
        (1, 1, None), (1, 2, None), (2, 1, 0), (2, 1, 1), (2, 1, 2)]
    result = validate_ndjson([first, second], tmp_path / "out")
    assert result.total == 5
    quarantine = [json.loads(s) for s in (tmp_path / "out" / "quarantine.ndjson").read_text().splitlines()]
    assert [r["source"] for r in quarantine] == [
        {"file_index": 1, "line": 2}, {"file_index": 2, "line": 1, "bundle_entry": 1},
        {"file_index": 2, "line": 1, "bundle_entry": 2}]
    for filename in ("issues.ndjson", "quarantine.ndjson", "operation-outcomes.ndjson"):
        assert "confidential-patient-file" not in (tmp_path / "out" / filename).read_text()


def test_invalid_completeness_policy_does_not_make_directory(tmp_path):
    with pytest.raises(ValueError):
        Policy(reference_scope="open", complete_resource_types=frozenset({"../secrets"}))
    with pytest.raises(ValueError):
        Policy(reference_scope="open", complete_resource_types=["Patient"])


def test_trusted_absolute_reference_can_resolve_to_local_patient_and_closure(tmp_path):
    f = tmp_path / 'absolute.ndjson'
    _dump(f, [
        {'resourceType': 'Patient', 'id': 'bad', 'birthDate': '2026-02-30'},
        {'resourceType': 'Observation', 'id': 'obs', 'status': 'final',
         'code': {'text': 'test'}, 'subject': {'reference': 'https://fhir.hospital.example/api/r4/Patient/bad'}}
    ])
    scope = Policy(reference_scope='open', trusted_reference_bases=('https://fhir.hospital.example/api/r4',))
    result = validate_ndjson([f], tmp_path / 'trusted', scope)
    assert result.quarantined == 2
    assert result.by_issue_code.get('DQ_REFERENCE_TARGET_QUARANTINED') == 1
    without = validate_ndjson([f], tmp_path / 'untrusted', Policy(reference_scope='open'))
    assert (without.accepted, without.quarantined) == (1, 1)


def test_trusted_absolute_reference_honors_exact_root_and_uri_boundaries(tmp_path):
    f = tmp_path / 'source.ndjson'
    _dump(f, [{"resourceType": "Patient", "id": "p"},
              {"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "test"},
               "subject": {"reference": "https://fhir.example.com/fhir-evil/Patient/p"},
               "performer": [{"reference": "https://fhir.example.com.evil/fhir/Organization/missing"}]}])
    pol = Policy(reference_scope='closed', trusted_reference_bases=('https://fhir.example.com/fhir',))
    result = validate_ndjson([f], tmp_path / 'out', pol)
    assert result.accepted == 2
    assert result.quarantined == 0
    assert result.by_issue_code['DQ_REFERENCE_NOT_CHECKED'] == 2


@pytest.mark.parametrize('base', ['http://example.org/fhir', 'https://example.org/fhir/',
                                   'https://user:pass@example.org/fhir',
                                   'https://example.org/fhir?x=1',
                                   'https://example.org//fhir',
                                   'https://example.org\\@bad/fhir'])
def test_rejects_untrusted_or_ambiguous_reference_base(base):
    with pytest.raises(ValueError):
        Policy(trusted_reference_bases=(base,))


def test_versioned_reference_is_not_silently_assumed_to_be_latest(tmp_path):
    from fhir_quality.readiness import assess_readiness
    f = tmp_path / 'version.ndjson'
    _dump(f, [{'resourceType': 'Patient', 'id': 'p', 'meta': {'versionId': '2'}},
              {'resourceType': 'Observation', 'id': 'o', 'status': 'final', 'code': {'text': 'test'},
               'subject': {'reference': 'Patient/p/_history/1'}}])
    result = validate_ndjson([f], tmp_path / 'out', Policy(reference_scope='closed'))
    assert result.accepted == 2 and result.quarantined == 0
    assert result.by_issue_code['DQ_VERSION_REFERENCE_NOT_CHECKED'] == 1
    # An accepted resource is NOT the same as a migration approved for release.
    gate = assess_readiness(result)
    assert gate['ready'] is False
    assert any(c['name'] == 'unverified_versioned_references' and not c['passed'] for c in gate['checks'])


def test_versioned_trusted_absolute_reference_is_also_unverified(tmp_path):
    f = tmp_path / 'version.ndjson'
    _dump(f, [{'resourceType': 'Patient', 'id': 'p'},
              {'resourceType': 'Observation', 'id': 'o', 'status': 'final', 'code': {'text': 'test'},
               'subject': {'reference': 'https://fhir.example.org/r4/Patient/p/_history/1'}}])
    scope = Policy(reference_scope='closed', trusted_reference_bases=('https://fhir.example.org/r4',))
    result = validate_ndjson([f], tmp_path / 'out', scope)
    assert result.accepted == 2 and result.by_issue_code['DQ_VERSION_REFERENCE_NOT_CHECKED'] == 1
