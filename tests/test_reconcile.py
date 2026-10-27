import json
from pathlib import Path
import pytest
from fhir_quality.reconcile import compare_snapshots

SECRET = b"this-is-a-demo-key-not-for-production"

def snapshot(tmp_path, name, rows):
    path = tmp_path / f"{name}.ndjson"
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return [path]


def test_safe_diff_and_change_accounting(tmp_path):
    old = snapshot(tmp_path, "old", [
        {"resourceType": "Patient", "id": "p1", "gender": "male"},
        {"resourceType": "Patient", "id": "p2"},
        {"resourceType": "Observation", "id": "o1", "status": "final"},
    ])
    new = snapshot(tmp_path, "new", [
        {"id": "p1", "gender": "female", "resourceType": "Patient"},
        {"resourceType": "Patient", "id": "p3"},
        {"resourceType": "Observation", "id": "o1", "status": "final"},
    ])
    result = compare_snapshots(old, new, secret=SECRET)
    assert result["safe_for_change_accounting"]
    assert result["totals"] == {"added": 1, "modified": 1, "unchanged": 1, "unobserved": 1}
    assert "p1" not in json.dumps(result) and "p3" not in json.dumps(result)
    completed = compare_snapshots(old, new, secret=SECRET, complete_snapshots=True)
    assert completed["totals"]["deleted"] == 1
    assert "unobserved" not in completed["totals"]


def test_ignoring_last_updated_is_explicit(tmp_path):
    a = snapshot(tmp_path, "a", [{"resourceType": "Patient", "id": "p", "meta": {"lastUpdated": "2026-01-01T00:00:00Z"}}])
    b = snapshot(tmp_path, "b", [{"resourceType": "Patient", "id": "p", "meta": {"lastUpdated": "2026-01-02T00:00:00Z"}}])
    assert compare_snapshots(a,b,secret=SECRET)["totals"]["modified"] == 1
    assert compare_snapshots(a,b,secret=SECRET,ignore_meta_last_updated=True)["totals"]["unchanged"] == 1


def test_duplicate_or_invalid_input_fails_accounting(tmp_path):
    a = snapshot(tmp_path, "a", [{"resourceType": "Patient", "id": "p"}]*2)
    b = snapshot(tmp_path, "b", [{"resourceType": "Patient", "id": "p"}, [1,2]])
    r = compare_snapshots(a,b,secret=SECRET)
    assert not r["safe_for_change_accounting"]
    assert r["duplicate_keys"]["previous"] == 1
    assert r["invalid_records"]["current"] == 1


def test_requires_secret_and_inputs(tmp_path):
    path = snapshot(tmp_path, "a", [{"resourceType": "Patient", "id": "p"}])
    with pytest.raises(ValueError):
        compare_snapshots(path, path, secret=b"short")
    with pytest.raises(ValueError):
        compare_snapshots([], path, secret=SECRET)
    with pytest.raises(FileNotFoundError):
        compare_snapshots([tmp_path/"missing"],path,secret=SECRET)


def test_json_object_order_is_not_a_change(tmp_path):
    a = snapshot(tmp_path, "a", [{"id":"x","resourceType":"Patient","active":True}])
    b = snapshot(tmp_path, "b", [{"active":True,"resourceType":"Patient","id":"x"}])
    assert compare_snapshots(a,b,secret=SECRET)["totals"]["unchanged"] == 1
