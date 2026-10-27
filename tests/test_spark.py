"""Run on Spark 3.4+ with Java 17; intentionally opt-in on free CI budgets."""
import pytest

pyspark = pytest.importorskip("pyspark")
from pyspark.sql import SparkSession
from fhir_quality.spark import validate_spark, spark_readiness
import json


@pytest.fixture(scope="module")
def spark():
    session = (SparkSession.builder.master("local[2]").appName("fhir-quality-integration")
               .config("spark.ui.enabled", "false").getOrCreate())
    yield session
    session.stop()


def frame(spark, resources):
    return spark.createDataFrame([(json.dumps(r),) if isinstance(r, dict) else (r,) for r in resources], ["resource_json"])


def test_distributed_validation_and_quarantine(spark):
    rows = [
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Patient", "id": "p2"},
        {"resourceType": "Encounter", "id": "e1", "status": "finished", "class": {"system": "x", "code": "AMB"}, "subject": {"reference": "Patient/p1"}},
        {"resourceType": "Observation", "id": "good", "status": "final", "code": {"text": "A"}, "subject": {"reference": "Patient/p1"}, "encounter": {"reference": "Encounter/e1"}},
        {"resourceType": "Observation", "id": "bad", "status": "final", "code": {"text": "A"}, "subject": {"reference": "Patient/p2"}, "encounter": {"reference": "Encounter/e1"}},
        {"resourceType": "Observation", "id": "missing", "status": "final", "code": {"text": "A"}, "subject": {"reference": "Patient/absent"}},
    ]
    result = validate_spark(frame(spark, rows), batch_id="testbatch", token_salt="test-nonce-must-be-at-least-24-chars")
    assert result.accepted.count() == 4
    assert result.quarantined.count() == 2
    assert {r.code for r in result.issues.collect()} == {"DQ_PATIENT_ENCOUNTER_MISMATCH", "DQ_PATIENT_REFERENCE_NOT_FOUND"}
    assert spark_readiness(result).count() == 2
    assert {r.rid for r in result.quarantined.collect()} == {"bad", "missing"}


def test_open_world_warning_not_quarantined(spark):
    rows = [{"resourceType": "Observation", "id": "o1", "status": "final", "code": {"text": "x"}, "subject": {"reference": "Patient/absent"}}]
    result = validate_spark(frame(spark, rows), batch_id="testbatch", token_salt="test-nonce-must-be-at-least-24-chars", reference_scope="open")
    assert result.accepted.count() == 1
    assert result.quarantined.count() == 0
    assert [(r.code, r.severity) for r in result.issues.collect()] == [("DQ_PATIENT_REFERENCE_NOT_FOUND", "warning")]


def test_duplicate_ids_both_rejected(spark):
    rows = [{"resourceType": "Patient", "id": "duplicate"}, {"resourceType": "Patient", "id": "duplicate"}]
    result = validate_spark(frame(spark, rows), batch_id="testbatch", token_salt="test-nonce-must-be-at-least-24-chars")
    assert result.quarantined.count() == 2
    assert result.accepted.count() == 0


def test_batch_id_rejected(spark):
    with pytest.raises(ValueError):
        validate_spark(frame(spark, []), batch_id="../../invalid", token_salt="test-nonce-must-be-at-least-24-chars")


def test_observation_choice_rules_and_source_lineage(spark):
    resource = {"resourceType":"Observation","id":"bad","status":"final", "code":{"text":"Hb"},
                "subject":{"reference":"Patient/p1"}, "valueString":"abnormal",
                "valueQuantity":{"value":4,"system":"http://unitsofmeasure.org"},
                "dataAbsentReason":{"text":"unknown"}}
    rows = spark.createDataFrame([(json.dumps({"resourceType":"Patient","id":"p1"}),"1"),
                                  (json.dumps(resource),"2")], ["resource_json","source_id"])
    result = validate_spark(rows,batch_id="t1",token_salt="long-nonce-value-for-testing-12345", row_id_column="source_id")
    assert result.quarantined.count() == 1
    assert result.accepted.count() == 1
    assert result.quarantined.select("source_row_id").first()[0] == "2"
    expected = {"FHIR_OBSERVATION_MULTIPLE_VALUES", "FHIR_OBSERVATION_VALUE_WITH_ABSENT_REASON", "DQ_UCUM_CODE_MISSING"}
    assert expected <= {r.code for r in result.issues.collect()}
