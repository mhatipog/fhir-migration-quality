# Changelog

## 0.4.0 — 2026-10-27

- Added per-resource-type extract completeness: missing Patients can be errors even when an Organization feed is incomplete.
- Added explicitly trusted HTTPS FHIR reference roots, with exact boundary checks and regression coverage.
- Stopped collapsing historical `/_history/{version}` references into current-resource IDs; unresolved versions are warnings that block release readiness.
- Added ordinal file, line and Bundle-entry locations to quarantines and issue reports, with no filesystem paths.
- Optimized non-Bundle reference resolution by bypassing impossible alias queries and caching immutable rejected-target lookups.
- Added deterministic invalid-patient cascade benchmark, input SHA-256 fingerprints and documented baseline-vs-candidate results.
- The full official MIMIC demo benchmark is still **not measured here**; do not confuse synthetic scale tests with real clinical results.

## 0.3.0 — 2026-10-27

- Replaced recursive reference scanning with an iterative traversal and a path-free dependency-indexing scan.
- Added bounded, instance-local SQLite lookup caches for frequent Patient and Encounter relationships.
- Added real clinical dataset compatibility smoke test and honest three-run synthetic baseline comparison (~1.19–1.20× throughput in two measured local workloads).
- Added a version-pinned, checksum-verified official MIMIC-IV-on-FHIR demo downloader and aggregate-only benchmark runner, plus manually triggered CI workflow.
- Deeply nested JSON now quarantines rather than terminating the batch.
- Full official real-data throughput remains **unmeasured** here due to blocked external dataset download.


## 0.2.0 — 2026-10-27

This release focuses on safety at the *dataset boundary*, not on claiming additional FHIR profiles.

- **Reference closure:** quarantine propagates to resources referencing rejected Patients, Encounters or other indexed resources. A recursive SQL traversal handles long chains and cycles without keeping the graph in Python memory.
- **Snapshot comparison:** compare two FHIR exports by logical resource ID and canonical content. HMACs keep source identifiers out of the reports. Deletions are never inferred from a partial snapshot.
- **Integrity inventory:** hash and count accepted FHIR NDJSON by resource type, then verify handoff files have not changed.
- **Bulk export inspection:** inspect a previously downloaded HL7 Bulk Data manifest and type-homogeneous resource files without network access.
- **Validation hardening:** refuse to silently round high-precision clinical JSON decimals; catch malformed status objects, conflicting Observation choice values, invalid issued instants, same-day reversed Encounter periods and missing UCUM unit codes.
- **Spark:** optional source row ID in returned DataFrames and additional native SQL Observation choice checks.
- **CLI:** `inventory`, `compare`, and `inspect-export` commands, with nonzero exit codes for failed gates.

Caveats: FHIR R4 only, not full HL7 profile/terminology validation. PySpark integration remains an optional test suite and requires a Spark runtime.

## 0.1.0 — 2026-10-27

Initial source preflight, deterministic CSV-to-FHIR mapping, resource/reference quality gate and Databricks quarantine helpers.