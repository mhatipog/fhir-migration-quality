import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'benchmarks' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_official_checksums_parser():
    checks = load('download_mimic').parse_checksums(
        'f' * 64 + '  fhir/MimicPatient.ndjson.gz\n' + 'a' * 64 + ' *./fhir/MimicEncounter.ndjson.gz\n'
    )
    assert checks['fhir/MimicPatient.ndjson.gz'] == 'f' * 64
    assert len(checks) == 2


def test_benchmark_summary_is_aggregate_only(tmp_path, capsys):
    src = tmp_path / 'patient.ndjson'
    src.write_text('{"resourceType":"Patient","id":"private_id"}\n')
    output = tmp_path / 'result.json'
    assert load('bench').main(['--input', str(src), '--repeat', '2', '--output', str(output)]) == 0
    raw = output.read_text()
    report = json.loads(raw)
    assert 'private_id' not in raw
    assert str(src) not in raw
    assert report['totals']['resources'] == 1
    assert report['median_resources_per_second'] > 0


def test_fingerprint_is_path_independent_and_no_source_names(tmp_path):
    import hashlib
    benchmark = load('bench')
    a, b = tmp_path / 'a-private-name.ndjson', tmp_path / 'b-private-name.ndjson'
    for x in (a, b):
        x.write_text('{"resourceType":"Patient","id":"not_for_report"}\n')
    assert benchmark.fingerprint(a) == benchmark.fingerprint(b)
    assert benchmark.fingerprint(a) == hashlib.sha256(a.read_bytes()).hexdigest()


def test_deterministic_rejection_stress_fixture(tmp_path):
    from fhir_quality.bulk import validate_ndjson
    generate = load('generate_synthetic').generate
    f = tmp_path / 'generated.ndjson'
    generate(f, 30, invalid_patient_every=2)
    out = validate_ndjson([f], tmp_path / 'out')
    assert out.total == 33
    assert out.quarantined == 22  # both invalid patients and their 20 dependent observations
    assert out.by_issue_code['DQ_REFERENCE_TARGET_QUARANTINED'] == 20
