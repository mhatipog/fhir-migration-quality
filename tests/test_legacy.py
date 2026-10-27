import csv
from pathlib import Path
import pytest
from fhir_quality.legacy import profile_legacy


def csv_file(path, records):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, records[0])
        writer.writeheader()
        writer.writerows(records)


def test_good_legacy_passes(tmp_path):
    csv_file(tmp_path / "patients.csv", [{"patient_id": "p1", "birth_date": "1978-09-04"}])
    csv_file(tmp_path / "encounters.csv", [{"encounter_id": "e1", "patient_id": "p1", "start": "2025-03-01T12:00:00Z"}])
    csv_file(tmp_path / "observations.csv", [{"observation_id": "o1", "patient_id": "p1", "encounter_id": "e1", "code_system": "s", "code": "c", "effective_at": "2025-03-01T12:30:00Z", "value": "12"}])
    report = profile_legacy(tmp_path)
    assert report["preflight_ready"]
    assert not report["issue_counts"]
    assert report["rows"]["patients"] == 1


def test_source_orphans_mismatch_and_duplicates(tmp_path):
    csv_file(tmp_path / "patients.csv", [{"patient_id": "p1"}, {"patient_id": "p1"}, {"patient_id": "p2"}])
    csv_file(tmp_path / "encounters.csv", [{"encounter_id": "e1", "patient_id": "p1", "start": "2024-01-01"}])
    csv_file(tmp_path / "observations.csv", [
        {"observation_id": "o1", "patient_id": "p2", "encounter_id": "e1", "code_system": "s", "code": "c", "effective_at": "2024-01-01", "value": "2"},
        {"observation_id": "o2", "patient_id": "absent", "encounter_id": "missing", "code_system": "s", "code": "c", "effective_at": "2024-01-01", "value": "2"}])
    r = profile_legacy(tmp_path)
    assert not r["preflight_ready"]
    assert r["issue_counts"]["SOURCE_DUPLICATE_KEY_ROWS"] == 2
    assert r["issue_counts"]["SOURCE_OBSERVATIONS_PATIENT_MISSING"] == 1
    assert r["issue_counts"]["SOURCE_OBSERVATION_ENCOUNTER_PATIENT_MISMATCH"] == 1
    assert r["issue_counts"]["SOURCE_OBSERVATION_ENCOUNTER_MISSING"] == 1
    assert "absent" not in str(r)


def test_schema_drift_and_column_map(tmp_path):
    csv_file(tmp_path / "patients.csv", [{"legacy_pk": "patient-1", "dob": "2020-01-01"}])
    no_map = profile_legacy(tmp_path)
    assert no_map["missing_required_columns"]["patients"] == ["patient_id"]
    with_map = profile_legacy(tmp_path, column_map={"patients": {"patient_id": "legacy_pk", "birth_date": "dob"}})
    assert with_map["preflight_ready"]


def test_empty_directory_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        profile_legacy(tmp_path)


def test_bad_birth_date(tmp_path):
    csv_file(tmp_path / "patients.csv", [{"patient_id": "p1", "birth_date": "2024-02-30"}])
    r = profile_legacy(tmp_path)
    assert r["issue_counts"]["SOURCE_BIRTHDATE_INVALID"] == 1


def test_missing_reference_tables_fail_closed(tmp_path):
    csv_file(tmp_path / "observations.csv", [{"observation_id": "o", "patient_id": "unknown", "encounter_id": "e",
                                              "code_system": "s", "code": "c", "effective_at": "2024-01-01", "value": "1"}])
    report = profile_legacy(tmp_path)
    assert report["issue_counts"]["SOURCE_PATIENT_DATASET_ABSENT"] == 1
    assert report["issue_counts"]["SOURCE_ENCOUNTER_DATASET_ABSENT"] == 1
    assert not report["preflight_ready"]
