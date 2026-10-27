"""Local FHIR NDJSON validation benchmark. Reports aggregate measurements only.

No network calls, no patient payloads or source identifiers in output. Benchmark
results depend on machine, storage, file mix, and reference-scope policy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

from fhir_quality.bulk import validate_ndjson
from fhir_quality.rules import Policy


def fingerprint(path: Path) -> str:
    """Stream a stable full-file hash; never emit input names, paths or row IDs."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_trial(paths: list[Path], scope: str, complete_types: frozenset[str] = frozenset(), trusted_bases: tuple[str, ...] = ()) -> dict:
    with tempfile.TemporaryDirectory(prefix="fhir-quality-benchmark-") as scratch:
        start = time.perf_counter()
        summary = validate_ndjson(paths, Path(scratch) / "out", Policy(reference_scope=scope, complete_resource_types=complete_types, trusted_reference_bases=trusted_bases))
        seconds = time.perf_counter() - start
        report = summary.as_dict()
        return {"seconds": seconds, "resources": report["total"],
                "accepted": report["accepted"], "quarantined": report["quarantined"],
                "warnings": report["warnings"], "issues_by_code": report["by_issue_code"],
                "resources_by_type": report["by_resource_type"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", nargs="+", required=True, help="Local NDJSON or NDJSON.gz paths; never uploaded")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--reference-scope", choices=("closed", "open"), default="open")
    ap.add_argument("--complete-resource-types", nargs="*", default=[], metavar="TYPE")
    ap.add_argument("--trusted-reference-base", action="append", default=[], metavar="HTTPS_URL")
    ap.add_argument("--dataset-label", default="unverified-local-files",
                    help="Operator-attested dataset identifier, e.g. mimic-iv-fhir-demo-2.1.0")
    ap.add_argument("--output", help="Optional aggregate-only JSON output (will not overwrite)")
    args = ap.parse_args(argv)
    if not 1 <= args.repeat <= 10:
        ap.error("repeat must be between 1 and 10")
    paths = sorted(Path(name) for name in args.input)
    if not all(f.is_file() for f in paths):
        ap.error("all inputs must exist")
    if any(not (p.name.endswith(".ndjson") or p.name.endswith(".ndjson.gz")) for p in paths):
        ap.error("only .ndjson and .ndjson.gz input files are accepted")
    if not all(c.isascii() and (c.isalnum() or c in "-._") for c in args.dataset_label) or len(args.dataset_label) > 72:
        ap.error("dataset-label must contain only ASCII letters, digits, dots, hyphens and underscores")
    if args.output and Path(args.output).exists():
        ap.error("output already exists; refusing overwrite")
    try:
        Policy(reference_scope=args.reference_scope, complete_resource_types=frozenset(args.complete_resource_types), trusted_reference_bases=tuple(args.trusted_reference_base))
    except ValueError as exc:
        ap.error(str(exc))
    trials = [run_trial(paths, args.reference_scope, frozenset(args.complete_resource_types), tuple(args.trusted_reference_base)) for _ in range(args.repeat)]
    for trial in trials[1:]:
        assert {k: v for k,v in trial.items() if k != "seconds"} == {
            k: v for k,v in trials[0].items() if k != "seconds"
        }, "Quality result differs between identical trials"
    seconds = [trial["seconds"] for trial in trials]
    n = trials[0]["resources"]
    output = {
        "schema_version": 2,
        "input_fingerprints": [
            {"ordinal": n, "bytes": path.stat().st_size, "sha256": fingerprint(path)}
            for n, path in enumerate(paths, 1)
        ],
        "dataset_label": args.dataset_label,
        "implementation_version": __import__("fhir_quality").__version__,
        "run_status": "measured",
        "data_origin": "operator-provided-local-files; user must separately attest dataset provenance",
        "input_file_count": len(paths),
        "input_bytes_compressed_or_raw": sum(p.stat().st_size for p in paths),
        "reference_scope": args.reference_scope,
        "complete_resource_types": sorted(args.complete_resource_types),
        "trusted_reference_base_count": len(args.trusted_reference_base),
        "repeat": args.repeat,
        "runtime": {"python": platform.python_version(), "platform": platform.system(),
                    "architecture": platform.machine(), "cpu_count": os.cpu_count()},
        "totals": {k:v for k,v in trials[0].items() if k!="seconds"},
        "timings_seconds": [round(x, 5) for x in seconds],
        "median_seconds": round(statistics.median(seconds), 5),
        "median_absolute_deviation_seconds": round(statistics.median(
            abs(t - statistics.median(seconds)) for t in seconds), 5),
        "median_resources_per_second": round(n / statistics.median(seconds), 2) if seconds[0] else None,
        # ru_maxrss is peak for this process; not reliably per trial.
        "peak_rss_kib_linux_process": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if platform.system()=="Linux" else None,
        "limitations": ["Not a full HL7 FHIR conformance test", "Peak RSS reflects the benchmarking process, not each trial",
                        "Open reference scope records missing targets as warnings", "Measures local end-to-end read/index/validate/write", "Input SHA-256 fingerprints are computed outside timed trials"]
    }
    text = json.dumps(output, sort_keys=True, indent=2) + "\n"
    if args.output:
        # Aggregate results still may disclose dataset-level properties: opt-in write.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
