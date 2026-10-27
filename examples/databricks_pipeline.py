"""Example job for Databricks; intended to be copied into a notebook or job task.

Install `fhir-migration-quality[spark]` on cluster beforehand.
"""
from fhir_quality.spark import validate_spark, spark_readiness

# Adapt for Unity Catalog volumes or ADLS Gen2. Keep datasets behind ACLs.
source_path = "/Volumes/healthcare/landing/fhir/patient_encounter_observation.ndjson"
output_root = "/Volumes/healthcare/quality/fhir"
batch_id = "20261027_b01"  # stable identifier for this input snapshot
# Use fresh, random, per-batch nonce (not a credential). Do not include PHI in job parameters.
run_nonce = "example-only-nonce-replace-with-random-uuid"

source = spark.read.text(source_path).withColumnRenamed("value", "resource_json")
result = validate_spark(source, batch_id=batch_id, token_salt=run_nonce, reference_scope="closed")

# Use a distinct path per batch to make reruns replace only that batch. Existing batches remain untouched.
result.accepted.write.format("delta").mode("overwrite").save(f"{output_root}/accepted/{batch_id}")
result.quarantined.write.format("delta").mode("overwrite").save(f"{output_root}/quarantine/{batch_id}")
spark_readiness(result).write.format("delta").mode("overwrite").save(f"{output_root}/metrics/{batch_id}")
# Token-only findings with rule identifiers; separate from PHI-bearing quarantine datasets.
result.issues.write.format("delta").mode("overwrite").save(f"{output_root}/issues/{batch_id}")
