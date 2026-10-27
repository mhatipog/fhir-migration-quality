"""Small, reproducible Spark FHIR validation benchmark with fabricated records."""
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

from pyspark.sql import SparkSession
from fhir_quality.spark import validate_spark


def sample(n):
    patients = n // 4
    records = []
    for i in range(patients):
        records.append({"resourceType": "Patient", "id": f"p{i}"})
        records.append({"resourceType": "Encounter", "id": f"e{i}",
                        "status": "finished",
                        "class": {"system": "urn:test", "code": "AMB"},
                        "subject": {"reference": f"Patient/p{i}"}})
    for i in range(n // 2):
        j = i % patients
        patient = (j + 1) % patients if i % 50 == 0 else j
        records.append({"resourceType": "Observation", "id": f"o{i}",
                        "status": "final", "code": {"text": "Synthetic"},
                        "subject": {"reference": f"Patient/p{patient}"},
                        "encounter": {"reference": f"Encounter/e{j}"},
                        "valueInteger": 12})
    return [json.dumps(r, separators=(",", ":")) for r in records]


def benchmark(spark, n, repetitions=2):
    frame = spark.createDataFrame([(x,) for x in sample(n)], ["resource_json"]).repartition(4).cache()
    frame.count()
    timings = []
    counts = []
    for _ in range(repetitions):
        start = time.perf_counter()
        result = validate_spark(
            frame, batch_id="sparkbenchmark",
            token_salt="fabricated-benchmark-salt-12345678")
        count = {"accepted": result.accepted.count(),
                 "quarantined": result.quarantined.count(),
                 "issues": result.issues.count()}
        timings.append(time.perf_counter() - start)
        counts.append(count)
    frame.unpersist()
    assert counts[0]["accepted"] + counts[0]["quarantined"] == n
    assert counts[0]["quarantined"] > 0
    assert counts[0] == counts[1], "Quality results varied between runs"
    median = statistics.median(timings)
    return {"resources": n, **counts[0],
            "timings_seconds": [round(t, 4) for t in timings],
            "median_seconds": round(median, 4),
            "median_resources_per_second": round(n / median, 2)}


def main():
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "spark-summary.json")
    if output.exists():
        raise SystemExit("Output exists; refusing overwrite")
    spark = (SparkSession.builder.master("local[2]")
             .appName("fhir-quality-benchmark")
             .config("spark.ui.enabled", "false")
             .config("spark.sql.shuffle.partitions", "4").getOrCreate())
    try:
        report = {"data_origin": "fabricated data, not patient records",
                  "spark_version": spark.version,
                  "python_version": platform.python_version(),
                  "cpu_count": os.cpu_count(),
                  "workloads": [benchmark(spark, n) for n in (400, 1600)],
                  "limitations": ["Spark local mode, not Azure Databricks",
                                  "Excludes startup and input materialization",
                                  "Includes execution of all three output counts"]}
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
