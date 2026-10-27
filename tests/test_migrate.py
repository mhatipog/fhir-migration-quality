import csv
from pathlib import Path
import pytest
from fhir_quality.migrate import MigrationConfig, MigrationError, convert_csv, stable_id, map_patient, map_encounter, map_observation
from fhir_quality.bulk import validate_ndjson

CFG = MigrationConfig(b"very-long-test-key-not-a-real-secret", "synthetic")


def test_deterministic_but_kind_separated():
    a = stable_id(CFG, "Patient", "001")
    assert a == stable_id(CFG, "Patient", "001")
    assert a != stable_id(CFG, "Encounter", "001")
    assert a != stable_id(MigrationConfig(b"different-test-key-for-this-test", "synthetic"), "Patient", "001")
    assert len(a) <= 64 and a.startswith("pat-")


def test_no_raw_patient_identifier_default():
    r = map_patient({"patient_id": "SECRET001", "birth_date": "1980-01-01", "gender": "female"}, CFG)
    assert "SECRET001" not in str(r)
    assert r["birthDate"] == "1980-01-01"
    assert "identifier" not in r


def test_identifier_system_optin():
    r = map_patient({"patient_id": "A"}, MigrationConfig(CFG.secret, "synthetic", "urn:facility:legacy"))
    assert r["identifier"][0]["value"] == "A"


@pytest.mark.parametrize("row,err", [
    ({"patient_id": "a", "gender": "x"}, "gender"),
    ({"patient_id": "", "gender": "male"}, "key"),
    ({"patient_id": "x", "birth_date": "2021-02-30"}, "date"),
])
def test_bad_patient(row, err):
    with pytest.raises(MigrationError):
        map_patient(row, CFG)


def test_encounter_links():
    enc = map_encounter({"encounter_id": "e", "patient_id": "p", "start": "2024-01-01T01:00:00Z"}, CFG)
    assert enc["subject"]["reference"] == "Patient/" + stable_id(CFG, "Patient", "p")
    assert enc["class"]["code"] == "AMB"


def test_observation_quantity_and_ucum():
    obs = map_observation({"observation_id": "o", "patient_id": "p", "encounter_id": "e",
                           "code_system": "http://loinc.org", "code": "15074-8", "effective_at": "2024-01-01T09:00:00Z",
                           "value": "5.5", "unit_code": "mmol/L"}, CFG)
    assert obs["valueQuantity"]["value"] == 5.5
    assert obs["valueQuantity"]["code"] == "mmol/L"
    assert obs["encounter"]["reference"] == "Encounter/" + stable_id(CFG, "Encounter", "e")


@pytest.mark.parametrize("value_type,value", [("boolean", "true"), ("boolean", "false"), ("string", "okay")])
def test_other_value_types(value_type, value):
    result = map_observation({"observation_id": "o", "patient_id": "p", "code_system": "http://loinc.org",
                              "code": "xx", "effective_at": "2024-01-01", "value_type": value_type, "value": value}, CFG)
    assert "value" + value_type.capitalize() in result


def test_nonfinite_and_invalid_unit_rejected():
    source = {"observation_id": "o", "patient_id": "p", "code_system": "http://loinc.org", "code": "xx", "effective_at": "2024-01-01", "value": "NaN"}
    with pytest.raises(MigrationError):
        map_observation(source, CFG)
    source["value"] = "2"
    source["unit_code"] = "abc"
    source["unit_system"] = "bad"
    with pytest.raises(MigrationError):
        map_observation(source, CFG)


def test_end_to_end_csv_migrate_then_validate(tmp_path):
    src = tmp_path / "legacy"
    src.mkdir()
    datasets = {
        "patients": [{"patient_id": "p", "birth_date": "1999-01-01", "gender": "female"}],
        "encounters": [{"encounter_id": "e", "patient_id": "p", "start": "2024-05-01T10:00:00Z"}],
        "observations": [{"observation_id": "o", "patient_id": "p", "encounter_id": "e", "code_system": "http://loinc.org", "code": "15074-8", "effective_at": "2024-05-01T11:00:00Z", "value": "5.4", "unit_code": "mmol/L"}],
    }
    for name, rows in datasets.items():
        with (src / (name + ".csv")).open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    report = convert_csv(src, tmp_path / "fhir.ndjson", CFG)
    assert report["mapped"] == 3 and report["rejected"] == 0
    summary = validate_ndjson([tmp_path / "fhir.ndjson"], tmp_path / "validated")
    assert summary.total == 3 and summary.accepted == 3 and summary.quarantined == 0


def test_secret_minimum_length():
    with pytest.raises(ValueError):
        MigrationConfig(b"short")


def test_env_secret_required(monkeypatch):
    monkeypatch.delenv("FHIR_MIGRATION_KEY", raising=False)
    with pytest.raises(ValueError):
        MigrationConfig.from_env()


def test_quantity_precision_guard():
    row = {"observation_id": "o", "patient_id": "p", "code_system": "http://loinc.org", "code": "x", "effective_at": "2026-01-01", "value": "1234567890123456789"}
    with pytest.raises(MigrationError):
        map_observation(row, CFG)


def test_missing_gender_cell_is_safe():
    assert map_patient({"patient_id": "p", "gender": None}, CFG)["resourceType"] == "Patient"


def test_column_renames_csv(tmp_path):
    src = tmp_path / "legacy"
    src.mkdir()
    (src / "patients.csv").write_text("ehr_id,gender\n123,female\n")
    output = tmp_path / "mapped.ndjson"
    report = convert_csv(src, output, CFG, column_map={"patients": {"patient_id": "ehr_id"}})
    assert report["mapped"] == 1
    import json
    assert json.loads(output.read_text())["gender"] == "female"
