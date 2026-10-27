import json
import pytest
from fhir_quality.export import inspect_downloaded_export


def configure(tmp_path, url='https://ehr.example.org/download/Patients.ndjson?signature=secret', resource_type='Patient', rows=None):
    root = tmp_path / 'download'
    root.mkdir()
    (root / 'Patients.ndjson').write_text(''.join(json.dumps(x)+'\n' for x in (rows if rows is not None else [{"resourceType":"Patient","id":"p"}])))
    manifest=tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({"transactionTime":"2026-10-27T12:00:00Z","requiresAccessToken": True,
                                    "output":[{"type":resource_type,"url":url}],"error": []}))
    return manifest, root


def test_offline_inspection_does_not_echo_url_token_or_id(tmp_path):
    manifest, folder = configure(tmp_path)
    summary=inspect_downloaded_export(manifest,folder)
    assert summary['files']==1 and summary['resources']==1
    assert summary['resources_by_type']=={'Patient':1}
    assert 'signature' not in json.dumps(summary)
    assert '"p"' not in json.dumps(summary)


def test_wrong_type_rejected(tmp_path):
    m,d=configure(tmp_path,resource_type='Observation')
    with pytest.raises(ValueError):
        inspect_downloaded_export(m,d)


def test_reject_url_path_traversal(tmp_path):
    m,d=configure(tmp_path,url='https://ehr.example.org/export/%2e%2e/Patients.ndjson')
    with pytest.raises(ValueError):
        inspect_downloaded_export(m,d)


def test_reject_duplicate_basename(tmp_path):
    m,d=configure(tmp_path)
    doc=json.loads(m.read_text())
    doc['output'].append({"type":"Patient","url":"https://second.example.org/download/Patients.ndjson"})
    m.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        inspect_downloaded_export(m,d)


def test_reject_local_symlink(tmp_path):
    m,d=configure(tmp_path)
    (d/'Patients.ndjson').unlink()
    outside=tmp_path/'outside.txt'
    outside.write_text('{"resourceType":"Patient","id":"p"}\n')
    (d/'Patients.ndjson').symlink_to(outside)
    with pytest.raises(ValueError):
        inspect_downloaded_export(m,d)


def test_manifest_malformed(tmp_path):
    m,d=configure(tmp_path)
    m.write_text('{"transactionTime":"2026-10-27","output":[]}')
    with pytest.raises(ValueError):
        inspect_downloaded_export(m,d)


def test_gzip_supported(tmp_path):
    import gzip
    m,d=configure(tmp_path,url='https://ehr.example.org/download/Patients.ndjson.gz')
    (d/'Patients.ndjson').unlink()
    with gzip.open(d/'Patients.ndjson.gz','wt') as f:
        f.write('{"resourceType":"Patient","id":"p"}\n')
    assert inspect_downloaded_export(m,d)['resources']==1
