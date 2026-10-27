"""Databricks/Spark-native FHIR R4 quality gate (no Python UDFs, no driver collect).

Deliberately a *bounded set* of migration rules. Use the official FHIR validator
for full profile/terminology/FHIRPath conformance, separately from this job.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SparkResult:
    accepted: Any
    quarantined: Any
    issues: Any
    # Views are lazy. Cache the source plan in callers when reusing all three.


def validate_spark(df: Any, *, json_column: str = "resource_json", batch_id: str,
                   token_salt: str, reference_scope: str = "closed",
                   row_id_column: str | None = None) -> SparkResult:
    """Validate raw NDJSON rows with native Spark SQL operations.

    Checks JSON/rtype/id, R4 Encounter/Observation status and required fields,
    duplicate ids, Patient and Encounter reference existence, and mismatched
    Observation/Encounter patient. Does NOT verify all FHIR resource types,
    CodeSystem bindings, arbitrary references, or contained resources.

    token_salt is an independent high-entropy *per-batch* salt for issue tokens.
    Do not use a static publicly known value if tokens will be shared.
    """
    try:
        from pyspark.sql import functions as F
    except ImportError as exc:
        raise ImportError("Install fhir-migration-quality[spark] for Spark helpers") from exc
    if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", batch_id):
        raise ValueError("batch_id must match [A-Za-z0-9_-]{1,80}")
    if not isinstance(token_salt, str) or len(token_salt) < 24:
        raise ValueError("token_salt must have at least 24 characters")
    if reference_scope not in ("closed", "open"):
        raise ValueError("reference_scope must be closed or open")

    if row_id_column is not None and (not isinstance(row_id_column, str) or not row_id_column):
        raise ValueError("row_id_column must be a nonempty string")
    columns = [F.col(json_column).cast("string").alias("resource_json")]
    if row_id_column is not None:
        columns.append(F.col(row_id_column).cast("string").alias("source_row_id"))
    raw = df.select(*columns)
    token_parts = [F.lit(token_salt), F.lit(":"), F.col("resource_json")]
    if row_id_column is not None:
        # Missing row ids are not silently interpreted as distinct source rows.
        token_parts.extend([F.lit(":"), F.coalesce(F.col("source_row_id"), F.lit("<missing>"))])
    choices = ("valueQuantity", "valueCodeableConcept", "valueString", "valueBoolean",
               "valueInteger", "valueRange", "valueRatio", "valueSampledData",
               "valueTime", "valueDateTime", "valuePeriod")
    # Get fields with Catalyst expressions rather than Python UDFs; no full JSON parse to driver.
    parsed = (raw
        .withColumn("rtype", F.get_json_object("resource_json", "$.resourceType"))
        .withColumn("rid", F.get_json_object("resource_json", "$.id"))
        .withColumn("status", F.get_json_object("resource_json", "$.status"))
        .withColumn("code_json", F.get_json_object("resource_json", "$.code"))
        .withColumn("code_coding", F.get_json_object("resource_json", "$.code.coding"))
        .withColumn("code_text", F.get_json_object("resource_json", "$.code.text"))
        .withColumn("class_code", F.get_json_object("resource_json", "$.class.code"))
        .withColumn("class_system", F.get_json_object("resource_json", "$.class.system"))
        .withColumn("subject", F.get_json_object("resource_json", "$.subject.reference"))
        .withColumn("encounter", F.get_json_object("resource_json", "$.encounter.reference"))
        .withColumn("record_token", F.sha2(F.concat(*token_parts), 256))
        .withColumn("batch_id", F.lit(batch_id)))
    for choice in choices:
        parsed = parsed.withColumn("_has_" + choice, F.get_json_object("resource_json", "$." + choice).isNotNull())
    parsed = (parsed
              .withColumn("_value_choice_count", sum((F.col("_has_" + c).cast("int") for c in choices), F.lit(0)))
              .withColumn("_has_absent_reason", F.get_json_object("resource_json", "$.dataAbsentReason").isNotNull())
              .withColumn("_ucum_system", F.get_json_object("resource_json", "$.valueQuantity.system"))
              .withColumn("_ucum_code", F.get_json_object("resource_json", "$.valueQuantity.code")))

    existing = parsed.where(F.col("rtype").isNotNull() & F.col("rid").isNotNull())
    duplicates = (existing.groupBy("rtype", "rid").count().where(F.col("count") > 1)
                  .select("rtype", "rid", F.lit(True).alias("is_duplicate")))
    joined = parsed.join(duplicates, ["rtype", "rid"], "left")

    # DataFrame references are checked only for simple local ResourceType/id links.
    patients = (existing.where(F.col("rtype") == "Patient")
                .select(F.col("rid").alias("patient_key")).dropDuplicates())
    joined = (joined
              .withColumn("subject_patient", F.when(F.col("subject").rlike(r"^Patient/[A-Za-z0-9.\-]{1,64}$"),
                   F.regexp_extract("subject", r"^Patient/(.+)$", 1)))
              .join(patients.withColumn("patient_exists", F.lit(True)),
                    F.col("subject_patient") == F.col("patient_key"), "left")
              .drop("patient_key"))
    encounters = (existing.where(F.col("rtype") == "Encounter")
                  .groupBy("rid").agg(F.min("subject").alias("encounter_subject"))
                  .withColumnRenamed("rid", "encounter_key")
                  .withColumn("encounter_exists", F.lit(True)))
    joined = (joined
              .withColumn("encounter_id", F.when(F.col("encounter").rlike(r"^Encounter/[A-Za-z0-9.\-]{1,64}$"),
                   F.regexp_extract("encounter", r"^Encounter/(.+)$", 1)))
              .join(encounters, F.col("encounter_id") == F.col("encounter_key"), "left"))
    errors = [
        F.when(F.col("rtype").isNull(), F.lit("FHIR_JSON_OR_TYPE_INVALID")),
        F.when(~F.coalesce(F.col("rtype").rlike(r"^[A-Z][A-Za-z0-9]{0,63}$"), F.lit(False)), F.lit("FHIR_RESOURCE_TYPE")),
        F.when(~F.coalesce(F.col("rid").rlike(r"^[A-Za-z0-9.\-]{1,64}$"), F.lit(False)), F.lit("DQ_ID_REQUIRED")),
        F.when(F.col("is_duplicate").isNotNull(), F.lit("DQ_DUPLICATE_LOGICAL_ID")),
        F.when((F.col("rtype") == "Observation") & ~F.coalesce(F.col("status").isin("registered", "preliminary", "final", "amended", "corrected", "cancelled", "entered-in-error", "unknown"), F.lit(False)), F.lit("FHIR_OBSERVATION_STATUS")),
        F.when((F.col("rtype") == "Observation") & (F.col("code_json").isNull() | (F.col("code_coding").isNull() & F.col("code_text").isNull())), F.lit("FHIR_OBSERVATION_CODE")),
        F.when((F.col("rtype") == "Observation") & (F.col("subject").isNull()), F.lit("DQ_OBSERVATION_SUBJECT")),
        F.when((F.col("rtype") == "Observation") & (F.col("_value_choice_count") > 1), F.lit("FHIR_OBSERVATION_MULTIPLE_VALUES")),
        F.when((F.col("rtype") == "Observation") & F.col("_has_absent_reason") &
               (F.col("_value_choice_count") > 0), F.lit("FHIR_OBSERVATION_VALUE_WITH_ABSENT_REASON")),
        F.when((F.col("rtype") == "Encounter") & ~F.coalesce(F.col("status").isin("planned", "arrived", "triaged", "in-progress", "onleave", "finished", "cancelled", "entered-in-error", "unknown"), F.lit(False)), F.lit("FHIR_ENCOUNTER_STATUS")),
        F.when((F.col("rtype") == "Encounter") & (F.col("class_code").isNull() | F.col("class_system").isNull()), F.lit("FHIR_ENCOUNTER_CLASS")),
        F.when((F.col("rtype").isin("Encounter", "Observation")) & F.col("subject_patient").isNotNull() & F.col("patient_exists").isNull(), F.lit("DQ_PATIENT_REFERENCE_NOT_FOUND")),
        F.when((F.col("rtype") == "Observation") & F.col("encounter_id").isNotNull() & F.col("encounter_exists").isNull(), F.lit("DQ_ENCOUNTER_REFERENCE_NOT_FOUND")),
        F.when((F.col("rtype") == "Observation") & F.col("encounter_exists").isNotNull() & F.col("subject_patient").isNotNull() &
               F.col("encounter_subject").rlike(r"^Patient/[A-Za-z0-9.\-]{1,64}$") &
               (F.col("subject") != F.col("encounter_subject")), F.lit("DQ_PATIENT_ENCOUNTER_MISMATCH")),
    ]
    warnings = [
        F.when((F.col("rtype") == "Observation") & (F.col("_ucum_system") == "http://unitsofmeasure.org") &
               F.col("_ucum_code").isNull(), F.lit("DQ_UCUM_CODE_MISSING")),
        F.when((F.col("rtype").isin("Encounter", "Observation")) & F.col("subject").isNotNull() &
               F.col("subject_patient").isNull(), F.lit("DQ_SUBJECT_REFERENCE_NOT_CHECKED")),
        F.when((F.col("rtype") == "Observation") & F.col("encounter").isNotNull() &
               F.col("encounter_id").isNull(), F.lit("DQ_ENCOUNTER_REFERENCE_NOT_CHECKED")),
    ]
    if reference_scope == "open":
        errors = errors[:-3] + [errors[-1]]
        # The two not-found checks become warnings under open-world policy.
        warnings.extend([
            F.when((F.col("rtype").isin("Encounter", "Observation")) & F.col("subject_patient").isNotNull() & F.col("patient_exists").isNull(), F.lit("DQ_PATIENT_REFERENCE_NOT_FOUND")),
            F.when((F.col("rtype") == "Observation") & F.col("encounter_id").isNotNull() & F.col("encounter_exists").isNull(), F.lit("DQ_ENCOUNTER_REFERENCE_NOT_FOUND")),
        ])
    def compact(items):
        return F.filter(F.array(*items), lambda x: x.isNotNull())
    assessed = (joined.withColumn("error_codes", compact(errors))
                .withColumn("warning_codes", compact(warnings)))
    selected = ["batch_id", "record_token", "rtype", "rid", "resource_json", "error_codes", "warning_codes"]
    if row_id_column is not None:
        selected.insert(2, "source_row_id")
    accepted = assessed.where(F.size("error_codes") == 0).select(*selected)
    quarantine = assessed.where(F.size("error_codes") > 0).select(*selected)
    issues = (assessed
              .select(*(["batch_id", "record_token", "source_row_id"] if row_id_column is not None else ["batch_id", "record_token"]),
                F.explode(F.concat(F.col("error_codes"), F.col("warning_codes"))).alias("code"), "error_codes")
        .select(*(["batch_id", "record_token", "source_row_id"] if row_id_column is not None else ["batch_id", "record_token"]),
                "code", F.when(F.array_contains("error_codes", F.col("code")), "error").otherwise("warning").alias("severity")))
    return SparkResult(accepted=accepted, quarantined=quarantine, issues=issues)


def spark_readiness(result: SparkResult) -> Any:
    """Lazy Spark DataFrame: frequency per severity and rule (no PHI)."""
    from pyspark.sql import functions as F
    return result.issues.groupBy("severity", "code").agg(F.count(F.lit(1)).alias("issue_count"))
