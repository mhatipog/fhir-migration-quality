# Reproducible benchmarks

## 0.4.0: extraction and quarantine workloads, 27 October 2026

The comparisons below are **actual sequential local measurements** on generated
records, not clinical data, and neither Spark nor HL7 official validator timing.
We compared the untouched v0.3.0 source archive with v0.4.0 on identical
fixture bytes (verified by SHA-256). Both implementations produced exactly the
same acceptance, quarantine, and issue totals on every workload.

| Fixture | FHIR resources | v0.3.0 median | v0.4.0 median | Median speed ratio | Quarantined |
|---|---:|---:|---:|---:|---:|
| compact | 33,000 | 2.835s | 2.674s | 1.06× | 0 |
| rich | 16,500 | 2.905s | 2.807s | 1.04× | 0 |
| quarantine | 16,500 | 3.408s | 2.702s | 1.26× | 330 |

Each result is the median of three full read/index/check/write trials on the
same shared Linux x86_64 container (Python 3.13.5); execution order and
other processes were not controlled. The raw timings and input fingerprints
are in [`benchmarks/results/local-v0.4.0-2026-10-27.json`](../benchmarks/results/local-v0.4.0-2026-10-27.json).
Three trials are not enough to claim a robust performance win on the lighter
inputs. The larger improvement on the quarantine workload is consistent with
the indexed reference-closure lookup optimization but must be validated on
additional hardware and data mixes.

**The third workload matters:** every 50th synthetic Patient is given an
invalid date. Any Observation referring to those Patients is quarantined through
the dependency graph. In that test there are 330 quarantined resources and
15,000 deliberately missing Organization references recorded as warnings under
open-world policy. This is a stress test of integrity behavior, not a clinical
interpretation or substitute for an official HL7 profile validator.

To reproduce with **only synthetic data**:

```bash
python benchmarks/generate_synthetic.py --output private/compact.ndjson --observations 30000
python benchmarks/generate_synthetic.py --output private/rich.ndjson --observations 15000 --rich
python benchmarks/generate_synthetic.py --output private/rejections.ndjson \
  --observations 15000 --rich --invalid-patient-every 50
python benchmarks/bench.py --input private/rejections.ndjson --repeat 3 \
  --dataset-label synthetic-rejection-test --output private/bench-summary.json
```

**Official real-data status:** the public MIMIC-IV Clinical Database Demo on
FHIR v2.1.0 remains the intended real-world workload. The dataset could not be
fetched in this environment, so no full-MIMIC throughput numbers are presented.
`benchmarks/download_mimic.py` and a manually triggered workflow can run it
with checksum verification on an authorized, network-enabled machine. See below.

---


The numbers below are measurements, not capacity estimates. They're reported
with the dataset, execution path and limitations so another engineer can try
reproducing them. We benchmark the *end-to-end* offline validator: JSON parsing,
three passes through the source, SQLite indexing, dependency closure and output
writing. These measurements are **not** official FHIR validation throughput or
Spark/Databricks throughput.

## Results: 27 October 2026

| Local input | Size | v0.2.0 median | v0.3.0 median | Throughput speedup |
|---|---:|---:|---:|---:|
| Generated patients + observations (compact) | 33,000 resources; ~8.1 MB | 3.830 s | 3.191 s | **1.20×** |
| Generated patients + nested observations (rich) | 16,500 resources; ~12 MB | 3.469 s | 2.907 s | **1.19×** |

Both comparisons used three complete validation runs per version, Python 3.13.5
on Linux x86_64 (5 reported CPUs), the *same files*, and `reference_scope=open`.
For both workloads, baseline and revised code returned the **same** accepted,
quarantined and warning counts. The rich workload includes deliberately
unresolved Organization references: 15,000 **warnings**, not invalid resources.
The peak process RSS readings are included in the machine-readable result;
there is no claim of memory improvement. This is a single-machine, small-N
experiment with sequential runs, not a controlled distributed benchmark.

`benchmarks/results/local-2026-10-27.json` is a source-controlled,
aggregate-only report. Raw input files and output/quarantine payloads are
excluded from Git. The changes came from measuring the profiler first:
we replaced repeated recursive reference-tree walking with an iterative scan
and added instance-local bounded caches for repeated patient/encounter lookups.
Independent path-pointer tests and the full local test suite pass.

### Real-world compatibility smoke

Three **unchanged de-identified FHIR records**, one each of Patient, Condition
and Observation, were retrieved from the [CSIRO FHIR Spark validator's
MIMIC-derived fixture](https://github.com/aehrc/fhir-validator-spark/tree/main/data/mimic-iv-demo-10)
and tested offline. Result: **3 accepted, 0 quarantined, 4 unresolved reference
warnings**. Missing references are expected when only three resources from a
much larger dataset are presented with open reference scope.

This is a tiny real-data *correctness check*, **not a real-data performance
benchmark**. No records or identifiers are copied into this repository.

### Official real-world FHIR performance benchmark: prepared, not run here

Primary source: **[MIMIC-IV Clinical Database Demo on FHIR v2.1.0](https://physionet.org/content/mimic-iv-fhir-demo/2.1.0/)**,
Bennett et al. (2025), PhysioNet, version 2.1.0. DOI: 10.13026/vphg-y548.
The openly accessible demo includes de-identified critical-care records and
FHIR R4 resources in compressed NDJSON under ODbL 1.0. The full clinical
corpus is separate and **credentialed**; don't treat it as an open download.

The execution environment used for this report **could not fetch files from
PhysioNet**. No throughput or quality counts for the official 2.1.0 dataset
have been measured yet, and no numbers are fabricated. The deterministic
runner is included so the experiment is executable in a network-enabled
workstation or by the manually triggered GitHub Actions workflow.

```bash
python -m pip install -e .
python benchmarks/download_mimic.py --destination private/mimic
python benchmarks/bench.py --input private/mimic/*.ndjson.gz \
  --repeat 3 --reference-scope open \
  --dataset-label mimic-iv-fhir-demo-2.1.0-full \
  --output benchmark-mimic-aggregates.json
```

Downloads use the *official versioned PhysioNet host*, a file allowlist from
`SHA256SUMS.txt`, checksums verified **before** files are placed on disk, and
restrictive local permissions. Use `--core` for a seven-file partial export,
but never interpret its unresolved references as invalid FHIR conformance.
`benchmark-mimic-aggregates.json` contains no patient payloads or IDs; it does
contain clinical dataset aggregate counts, so review local policy before
publishing it. No data is transmitted by the validator.

The workflow `.github/workflows/mimic-benchmark.yml` is manual (`workflow_dispatch`).
It downloads the open demo, verifies checksums, runs the benchmark and uploads
**only the aggregate JSON** as a short-lived Actions artifact. It does not
commit clinical data, export patient records or change production deployments.

## How to reproduce the baseline-vs-revised comparison

1. Use the original v0.2.0 source archive or Git revision for the baseline.
2. Generate identical synthetic inputs in a private working directory:

```bash
python benchmarks/generate_synthetic.py --output private/compact.ndjson --observations 30000
python benchmarks/generate_synthetic.py --output private/rich.ndjson --observations 15000 --rich
```

3. Set `PYTHONPATH` to each version's `src` directory and invoke
   `python benchmarks/bench.py --input private/compact.ndjson --repeat 3`
   with different output filenames. Repeat for the richer dataset.
4. Compare `totals` for exact equality; then compare median wall time.

### Interpretation

An accepted resource passed only this project's explicit offline subset and
migration policies, not the HL7 Java validator or clinical review. Counts
from an incomplete dataset measure *that extraction scope only*. For production
benchmarks also vary reference coverage, profiles, storage, nodes, gzip ratio,
Delta writes, and input skew; separately measure time and cost for the full
HL7 validator and terminology server. Do not expose real clinical payloads in
logs, benchmark notebooks, CI artifacts or dashboards.
