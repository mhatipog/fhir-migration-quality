"""Offline reproducible *synthetic* throughput experiment.

Run `PYTHONPATH=src python examples/benchmark.py --observations 20000`.
"""
from __future__ import annotations
import argparse
import json
import tempfile
import time
from pathlib import Path
from fhir_quality.bulk import validate_ndjson


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--observations", type=int, default=20000)
    args = parser.parse_args()
    if not 1 <= args.observations <= 2_000_000:
        parser.error("observations must be between 1 and 2000000")
    with tempfile.TemporaryDirectory(prefix="fhir-bench-") as tmp:
        base = Path(tmp)
        source = base / "resources.ndjson"
        with source.open("w") as f:
            for i in range(max(1, args.observations // 10)):
                f.write(json.dumps({"resourceType": "Patient", "id": f"p{i}"}) + "\n")
            for i in range(args.observations):
                obj = {"resourceType": "Observation", "id": f"o{i}", "status": "final",
                       "code": {"coding": [{"system": "http://loinc.org", "code": "15074-8"}]},
                       "subject": {"reference": f"Patient/p{i % max(1, args.observations // 10)}"},
                       "valueQuantity": {"value": 4.9, "system": "http://unitsofmeasure.org", "code": "mmol/L"}}
                f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        t0 = time.perf_counter()
        result = validate_ndjson([source], base / "out")
        seconds = time.perf_counter() - t0
        print(json.dumps({"resources": result.total, "accepted": result.accepted,
                          "quarantined": result.quarantined, "wall_seconds": round(seconds, 3),
                          "resources_per_second": round(result.total / max(seconds, 0.00001)),
                          "input_bytes": source.stat().st_size}, indent=2))


if __name__ == "__main__":
    main()
