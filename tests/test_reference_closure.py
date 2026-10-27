import json
from fhir_quality.bulk import validate_ndjson
from fhir_quality.rules import Policy


def test_rejects_transitive_dependents_of_quarantined_patient(tmp_path):
    input_file=tmp_path/'raw.ndjson'
    input_file.write_text(''.join(json.dumps(r)+'\n' for r in [
        {"resourceType":"Patient","id":"p","birthDate":"2026-02-30"},
        {"resourceType":"Encounter","id":"e","status":"finished","class":{"system":"s","code":"AMB"},
         "subject":{"reference":"Patient/p"}},
        {"resourceType":"Observation","id":"o","status":"final","code":{"text":"Hb"},
         "subject":{"reference":"Patient/p"},"encounter":{"reference":"Encounter/e"}},
        {"resourceType":"Patient","id":"unrelated"},
    ]))
    summary=validate_ndjson([input_file],tmp_path/'out')
    assert (summary.accepted, summary.quarantined)==(1,3)
    assert summary.by_issue_code['DQ_REFERENCE_TARGET_QUARANTINED']>=2
    accepted=(tmp_path/'out'/'accepted'/'Patient.ndjson').read_text()
    assert 'unrelated' in accepted
    assert not (tmp_path/'out'/'accepted'/'Observation.ndjson').exists()


def test_reference_cycle_without_invalid_root_is_ok(tmp_path):
    input_file=tmp_path/'source.ndjson'
    resources=[{"resourceType":"Patient","id":"a","link":[{"other":{"reference":"Patient/b"},"type":"seealso"}]},
               {"resourceType":"Patient","id":"b","link":[{"other":{"reference":"Patient/a"},"type":"seealso"}]}]
    input_file.write_text(''.join(json.dumps(x)+'\n' for x in resources))
    result=validate_ndjson([input_file],tmp_path/'out')
    assert result.accepted==2 and result.quarantined==0


def test_duplicate_reference_target_cascades(tmp_path):
    input_file=tmp_path/'source.ndjson'
    rows=[{"resourceType":"Patient","id":"p"},{"resourceType":"Patient","id":"p"},
          {"resourceType":"Observation","id":"o","status":"final","code":{"text":"x"},"subject":{"reference":"Patient/p"}}]
    input_file.write_text(''.join(json.dumps(x)+'\n' for x in rows))
    result=validate_ndjson([input_file],tmp_path/'out')
    assert result.quarantined==3
    assert result.by_issue_code['DQ_REFERENCE_TARGET_QUARANTINED']==1


def test_open_world_still_rejects_known_bad_reference(tmp_path):
    f=tmp_path/'source.ndjson'
    rows=[{"resourceType":"Patient","id":"p","birthDate":"bad"},
          {"resourceType":"Observation","id":"o","status":"final","code":{"text":"test"},"subject":{"reference":"Patient/p"}}]
    f.write_text(''.join(json.dumps(x)+'\n' for x in rows))
    res=validate_ndjson([f],tmp_path/'out',Policy(reference_scope='open'))
    assert res.quarantined==2
