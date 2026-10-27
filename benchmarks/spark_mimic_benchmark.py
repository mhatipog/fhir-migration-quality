"""Benchmark the Spark quality gate on the official public MIMIC-IV FHIR demo core.

The workflow downloads and SHA-256 verifies the seven compressed files first.
Only aggregate counts and execution times are saved; no resource payloads or
source identifiers are sent to artifacts, reports, or console output.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time
from pathlib import Path

CORE = {
    "MimicPatient.ndjson.gz", "MimicEncounter.ndjson.gz",
    "MimicEncounterED.ndjson.gz", "MimicEncounterICU.ndjson.gz",
    "MimicCondition.ndjson.gz", "MimicObservationED.ndjson.gz",
    "MimicObservationLabevents.ndjson.gz",
}


def checked_paths(directory: Path) -> list[Path]:
    found = sorted(directory.glob("*.ndjson.gz"))
    actual = {p.name for p in found}
    if actual != CORE or len(found) != len(CORE) or any(not p.is_file() or p.is_symlink() for p in found):
        raise ValueError("Expected exactly the seven regular MIMIC-IV FHIR demo core files")
    return found


def measure(spark, source, label: str, repeat: int) -> dict:
    from fhir_quality.spark import validate_spark

    # Repeated trials use the same cached, materialized input DataFrame.
    frame = source if label == "all" else source.limit(int(label)).cache()
    n = frame.count()
    if n == 0:
        raise ValueError("No records in Spark benchmark input")
    trials = []
    for _ in range(repeat):
        start = time.perf_counter()
        result = validate_spark(
            frame, batch_id="public_mimic_core_benchmark",
            token_salt="public-sample-benchmark-nonce-not-for-real-patient-data",
            reference_scope="open",
        )
        accepted = result.accepted.count()
        quarantined = result.quarantined.count()
        # Only aggregate rule names and severities return to the driver.
        by_issue = {
            row["severity"] + ":" + row["code"]: row["count"]
            for row in result.issues.groupBy("severity", "code").count().collect()
        }
        seconds = time.perf_counter() - start
        if accepted + quarantined != n:
            raise AssertionError("Accepted + quarantined != input resources")
        trials.append({"seconds": seconds, "accepted": accepted,
                       "quarantined": quarantined, "issues_by_code": by_issue})
    first = trials[0]
    if any({k: v for k, v in t.items() if k != "seconds"} !=
           {k: v for k, v in first.items() if k != "seconds"} for t in trials[1:]):
        raise AssertionError("Quality totals varied on the same cached input")
    duration = [v["seconds"] for v in trials]
    if frame is not source:
        frame.unpersist()
    return {
        "scope": "all-seven-core-files" if label == "all" else "cached-prefix-subset",
        "requested_limit": label,
        "resources": n,
        "accepted": first["accepted"],
        "quarantined": first["quarantined"],
        "issues_by_code": first["issues_by_code"],
        "timings_seconds": [round(v, 4) for v in duration],
        "median_seconds": round(statistics.median(duration), 4),
        "median_resources_per_second": round(n / statistics.median(duration), 2),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--sizes", nargs="+", default=["10000", "all"])
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output exists; refusing to overwrite")
    if args.repeat not in (2, 3):
        parser.error("Repeat must be 2 or 3")
    if len(args.sizes) > 3 or len(set(args.sizes)) != len(args.sizes) or any(
            x != "all" and (not x.isdecimal() or not 1 <= int(x) <= 250000) for x in args.sizes):
        parser.error("Sizes must be unique limits up to 250000 or 'all'")
    paths = checked_paths(args.input_dir)

    from pyspark.sql import SparkSession
    from pyspark import StorageLevel

    spark = (SparkSession.builder.master("local[2]")
             .appName("fhir-quality-public-mimic-core-benchmark")
             .config("spark.ui.enabled", "false")
             .config("spark.sql.shuffle.partitions", "4")
             .getOrCreate())
    try:
        spark.sparkContext.setLogLevel("ERROR")
        start = time.perf_counter()
        source = (spark.read.text([str(p) for p in paths])
                  .selectExpr("value as resource_json")
                  .repartition(4).persist(StorageLevel.MEMORY_AND_DISK))
        total = source.count()
        read_seconds = time.perf_counter() - start
        results = [measure(spark, source, size, args.repeat) for size in args.sizes]
        report = {
            "schema_version": 1,
            "data_origin": "official public MIMIC-IV-on-FHIR demo 2.1.0 core; checksum verified by workflow",
            "dataset_scope": "seven demo core files; not the full 30-file dataset",
            "compressed_bytes": sum(p.stat().st_size for p in paths),
            "input_file_count": len(paths),
            "input_resources": total,
            "spark_version": spark.version,
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "spark_master": "local[2]",
            "reference_scope": "open",
            "repetitions": args.repeat,
            "input_materialization_seconds": round(read_seconds, 4),
            "workloads": results,
            "limitations": [
                "Spark local[2] on GitHub Actions, not Azure Databricks",
                "Not full HL7 R4 or profile/terminology conformance",
                "Spark rules are deliberately narrower than the offline Python validator",
                "Warm cached inputs; excludes download, Spark startup, and initial input materialization from timed trials",
                "Prefix subsets are not stratified and may omit reference targets; do not use their issue counts as clinical quality rates",
                "No concurrency/skew/recovery/Delta writes or external FHIR server measured",
                "Each timed trial includes accepted/quarantined count actions and aggregate issue-code count action",
            ],
        }
        # Refuse overwriting an existing aggregate file.
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, sort_keys=True)
            output.write("\n")
        print("Spark core benchmark completed; aggregate measurements only")
        print(json.dumps({"source_resources": total, "workloads": [
            {"resources": r["resources"], "median_seconds": r["median_seconds"],
             "accepted": r["accepted"], "quarantined": r["quarantined"]}
            for r in results
        ]}, sort_keys=True))
        source.unpersist()
    finally:
        spark.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
