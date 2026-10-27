# FHIR Migration Quality

**FHIR R4 migration checks for healthcare data engineering.** A Python library and CLI for legacy EHR/LIS exports, HL7 FHIR NDJSON, integrity reconciliation, Spark/Databricks quarantine, and repeatable release gates.

A row can be valid JSON and still point to the wrong patient's encounter. A second migration run can also silently drop records. This project checks those failure modes *around* an HL7 FHIR validator, rather than pretending to replace it.

See [measured benchmark results](docs/benchmarks.md), the reproducible [MIMIC-IV real-data benchmark](benchmarks/download_mimic.py), and the [hospital migration runbook](docs/migration-runbook.md). Start with a synthetic example: `bash examples/run_demo.sh`. The main import is `from fhir_quality import validate_ndjson, compare_snapshots`.

## What it handles

| Problem in a migration | How this package helps |
|---|---|
| Legacy EHR/LIS schema and key drift | Preflight CSV source keys, foreign keys, required columns, dates and code coverage before conversion |
| Stable resource identity | Deterministic, keyed IDs for Patient, Encounter and Observation; configurable source-column crosswalk |
| FHIR R4 NDJSON with broken links | Streaming offline validation with an SQLite index, contained/relative/Bundle `fullUrl` lookup, explicitly trusted absolute reference bases and per-type extraction completeness |
| A resource points at a rejected resource | **Transitive quarantine propagation**; accepted records cannot depend on a quarantined resource in the checked local graph |
| Bad or suspicious clinical data | Observation choice constraints, UCUM code completeness warning, time ordering, birth-date chronology, patient/encounter mismatch |
| Safe reruns and incremental loads | Bounded-memory snapshot comparisons: added, modified, unchanged, **unobserved**; deletion candidates only with explicit complete-snapshot assertion |
| Handover and reproducibility | Per-file SHA-256, byte sizes and resource counts; subsequent verification catches changed files. Quarantine includes ordinal file/line/Bundle-entry positions, never source paths |
| FHIR Bulk Data export handoff | Offline inspection of a downloaded HL7 Bulk Data manifest and its local files; no URL fetching or token logging |
| Databricks / Azure Spark pipelines | Native Spark expressions and joins, quality issue DataFrames, source-row lineage option, quarantine patterns; no Python row UDFs |

This is intentionally **not** a FHIR R4 conformance certificate, FHIR server, medical device, terminology service, patient-matching system, or clinical decision-support tool. The official HL7 validator and site-specific Implementation Guides remain a separate, mandatory validation stage for clinical migrations. See [scope and limitations](docs/migration-architecture.md).

## Installation

```bash
python -m pip install -e .
python -m pip install -e '.[spark]'  # only in a Spark/Databricks environment
```

Minimum Python 3.10. **Core ETL has no required third-party dependencies.** Spark 3.4+ is optional. The package is initially a repository distribution; it is not claimed to be published on PyPI.

## Try it with fabricated patients

```bash
bash examples/run_demo.sh
```

This runs with wholly artificial records. Example result: 3 Patients + 2 Encounters + 4 Observations. One Observation refers to a nonexistent patient; another has a patient/encounter mismatch. Both should be quarantined. Inspect `gated/summary.json`, `gated/issues.ndjson`, and `gated/accepted/Patient.ndjson` in the printed temporary directory.

For your own files:

```bash
fhir-quality profile-legacy --input ./legacy --fail-on-issues
export FHIR_MIGRATION_KEY='use-a-real-secret-from-your-secret-manager-minimum-16-bytes'
fhir-quality migrate-csv --input ./legacy --output ./private/migrated.ndjson --namespace hospital2026
fhir-quality validate --input ./private/migrated.ndjson --output ./private/run-001 \
  --reference-scope closed --fail-on-readiness --min-pass-rate 0.99
```

### Partial exports and trusted FHIR server references

For a hospital migration where Patients and Encounters are fully extracted but
Organizations, Practitioners and other resource types are only partially available:

```bash
fhir-quality validate \
  --input private/export/*.ndjson.gz \
  --output private/validation-001 \
  --reference-scope open \
  --complete-resource-types Patient Encounter \
  --trusted-reference-base https://fhir.example.org/r4 \
  --fail-on-readiness
```

The `complete-resource-types` declaration is an **operator assertion** about
extract completeness, not something the tool can infer. Missing references to
those types are errors; others remain warnings. Only an explicitly configured
HTTPS base can be normalized to a local `ResourceType/id` reference. Exact
Bundle aliases still take precedence, and lookalike domains/paths stay external.

Historical references (`Patient/123/_history/4`) are deliberately *not* resolved
against a latest-ID-only index. They produce
`DQ_VERSION_REFERENCE_NOT_CHECKED` and block the readiness gate. Validate them
with a version-aware FHIR store before release. Accepted resource output is
**not** a claim of release approval. Output issue rows identify their origin by
`file_index`, `line` and optionally `bundle_entry` to aid investigation without
putting source filenames in diagnostic payloads.

`profile-legacy` preflights source schema drift, missing and duplicate keys, broken patient/encounter joins, invalid dates and missing observation codes *before* any mapping is performed, reporting only aggregate counts. It fails closed when referenced source tables are unavailable. `migrate-csv` accepts any subset of `patients.csv`, `encounters.csv`, `observations.csv`. Source columns can be mapped using `--column-map examples/column_map.json`. Unknown or unmappable rows are counted as `rejected` (no raw values in console diagnostics). `convert_csv` does not itself produce a row-level input quarantine file, to avoid creating a second PHI copy. Fix input rows at source, then rerun; the bulk validator handles resource-level quarantine.

**Important:** use a *stable securely stored* HMAC key during incremental migrations. Changing it or the namespace changes FHIR IDs and can break relationships across batches. Never put this key in Git, command-line arguments, notebook output, or Spark job parameters.

### Migration column contract

| File | Required columns | Useful optional columns |
|---|---|---|
| `patients.csv` | `patient_id` | `birth_date`, `gender` (`female`, `male`, `other`, `unknown`) |
| `encounters.csv` | `encounter_id`, `patient_id`, `start` | `end`, `status`, `class` (`ambulatory`, `inpatient`, `emergency`, `virtual`) |
| `observations.csv` | `observation_id`, `patient_id`, `code_system`, `code`, `effective_at`, `value` | `encounter_id`, `value_type` (`quantity`, `string`, `boolean`), `unit_code`, `unit_display`, `status`, `display` |

**Do not infer clinical equivalence from columns.** Ensure source terminology, mappings, conversion of dates/time zones, codes, units, identity rules, consent/access restrictions and IG profiles are clinically reviewed. `code_system` is taken as supplied and not validated against the remote terminology authority. Numeric quantities with more than 15 significant decimal digits are rejected rather than silently rounded by a float conversion. Independently, the NDJSON quality gate quarantines JSON decimals that cannot survive a Python float round trip without changing their numeric value; it does **not** silently round incoming clinical measurements. If a source requires lossless high-precision JSON decimals, use a decimal-preserving serializer upstream rather than relaxing this check.

## Outputs: usable in production ETL

The validator refuses to overwrite a destination (prevents mixing different batches).

```text
private/run-001/
  accepted/
    Patient.ndjson          # one FHIR resource type per file
    Encounter.ndjson
    Observation.ndjson
  quarantine.ndjson         # {record_index,resource} internal envelopes; PHI-bearing
  issues.ndjson             # {record_index,issues}; code/path/description without raw values
  operation-outcomes.ndjson # {record_index,outcome} FHIR OperationOutcome inside an internal envelope
  summary.json              # totals, issue frequency, release-readiness checks
```

Only the `accepted/<ResourceType>.ndjson` files are suitable for further *type-separated* bulk processing. The quarantine, issues, and outcome *envelopes* are **not** themselves FHIR Bulk Data NDJSON resources. They use record indices to avoid exposing source IDs in diagnostics. Missing/invalid JSON is not echoed into quarantine. Every output file uses mode `0600` on POSIX in a new mode `0700` directory; an ephemeral SQLite reference index is cleaned up. Treat **all** outputs and intermediate storage as PHI-sensitive anyway. The validator makes no external API requests or telemetry calls.

Use `--reference-scope closed` for a full migration batch where local refs must exist; use `open` when a referenced patient/encounter may reside in an external corpus. Open mode makes unresolved relative references warnings, not assertions of validity. Duplicate logical IDs in this batch quarantine *all* occurrences. Reusing an identifier across two different Patient IDs is a strict local *identity collision* policy, not proof of an actual clinical identity collision.

`summary.json` additionally includes `readiness.ready` and a list of check/threshold results. This operational gate is independent of and much narrower than acceptance of a clinical migration.

### Inventory, snapshot comparisons and HL7 Bulk Data exports

Generate and later verify an offline inventory of the **accepted** resources. No source IDs or patient values are written to the inventory, but file hashes can still be sensitive correlation material.

```bash
fhir-quality inventory create --output ./private/run-001
fhir-quality inventory verify --output ./private/run-001
```

Compare two snapshots using a stable **independent** HMAC key from a secret store. An unobserved resource is *not* automatically a deletion. Avoid `--complete-snapshots` unless both extractions are fully scoped and reconciled.

```bash
export FHIR_RECONCILE_KEY='demo-only-replace-from-vault-minimum-16-bytes'
fhir-quality compare --previous ./private/last/accepted/Patient.ndjson \
  --current ./private/this/accepted/Patient.ndjson --fail-on-changes
# Include --complete-snapshots only if both snapshots contain the same complete scope.
```

The comparator produces aggregate counts only; malformed records and duplicate keys set `safe_for_change_accounting=false` and exit 2 in CLI mode. It never runs deletes or writes patient identifiers. Canonical JSON comparison ignores property order; `--ignore-last-updated` is an explicit opt-in for transfer-only timestamp changes.

For a previously downloaded HL7 Bulk Data `$export` (not an automatic HTTP client), inspect the manifest and local files:

```bash
fhir-quality inspect-export --manifest ./private/export-manifest.json --downloads ./private/downloads
```

This validates the manifest timestamp and output types, rejects unsafe/ambiguous local paths, and checks each file contains the declared resource type. Signed URLs and access tokens are never fetched or copied to the result. For the official wire format and meaning of manifest entries see the [Bulk Data Access IG](https://hl7.org/fhir/uv/bulkdata/). An internal `inventory.json` is **not** itself an HL7 Bulk Data export manifest.

## Azure Databricks: distributed quarantine

```python
from fhir_quality.spark import validate_spark, spark_readiness

source = spark.read.text("/Volumes/my_catalog/raw/fhir/Observation.ndjson").withColumnRenamed("value", "resource_json")
# In an actual migration, union with Patient and Encounter inputs BEFORE reference checking.
result = validate_spark(
    source,
    batch_id="oct_2026_01",
    token_salt="random-per-batch-nonce-at-least-24-chars",
    reference_scope="closed",
)

result.accepted.write.format("delta").mode("overwrite").save("/Volumes/my_catalog/quality/run-001/accepted")
result.quarantined.write.format("delta").mode("overwrite").save("/Volumes/my_catalog/quality/run-001/quarantine")
spark_readiness(result).show()
```

This transform is Spark-native: **no Python row UDF**, no `collect()` in the library, no HTTP requests, and no implicit writes or Delta table modification. It returns lazy DataFrames. Issue rows contain a salted content hash token rather than a source patient ID, but do not assume hashes are anonymous. Use a random per-batch *non-secret* nonce: Spark logical plans may reveal literal values. Secure Spark UI, driver logs, table ACLs, Unity Catalog privileges, and storage. The example (`examples/databricks_pipeline.py`) uses a *distinct output directory per batch*, rather than overwriting a shared table. For durable incremental loads implement target-specific Delta `MERGE`, retries and exactly-once orchestration externally.

Spark checks a **strict subset** of Python validations: resourceType, id, status, Encounter class presence, Observation code presence, duplicate resource keys, basic Patient/Encounter local links and Observation/Encounter subject mismatch. It **does not** evaluate the Python date/identifier rules, nested arbitrary references, Bundle URNs, official profiles, terminology or full FHIR structure. It is for **batch DataFrames**; streaming stateful join/watermark semantics have not been implemented. When using a partial export, use `reference_scope='open'` or join in required dimension resources beforehand.

## Validation rules and scope

Rule names beginning `FHIR_` cover explicitly implemented base fields and JSON shape only, not full FHIR conformance; `DQ_` rules are business/migration policy. Reference checks cover `Reference.reference` in the traversed JSON; references identified by `identifier` without `reference`, canonical URLs, cross-server resolution, and conditional references are not locally resolved. Bundled `urn:uuid`/`fullUrl` aliases are scoped to their source Bundle. Relative versioned references resolve to the logical resource ID (not a specific historical version).

Readiness is gated by nonempty input, minimum batch pass rate (99% default), missing refs, duplicate IDs, duplicate Patient identifiers and Patient/Encounter mismatches. Customize these via `ReadinessThresholds` and `assess_readiness`. A green readiness indicator **does not certify FHIR compatibility, clinical correctness, privacy compliance, or completeness**.

For official validation of a selected output sample, use the [HL7 FHIR Validator](https://hl7.org/fhir/validator/) pinned to **R4 4.0.1** and relevant national/organizational Implementation Guides. FHIR `StructureDefinition`s supply the real profiles, cardinality, invariants and terminology bindings; using a few local rules is not a substitute. A separate official-validator step in CI/secure infrastructure is the next interoperability gate.

## Real-world data and benchmarking

The project includes a version-pinned, SHA-256-verified downloader for the
[open MIMIC-IV-on-FHIR demo](https://physionet.org/content/mimic-iv-fhir-demo/2.1.0/),
a three-run local benchmark, seeded synthetic workloads, and a manual GitHub
Actions benchmark. On this machine, synthetic workloads improved approximately
**19–20% in throughput** compared with v0.2.0; 3 public MIMIC-derived records
also passed a compatibility smoke test. **The larger official MIMIC demo was not
run in this environment** because the data host could not be reached. See
[full evidence and reproduction steps](docs/benchmarks.md) before quoting results.

Raw healthcare data stays in `private/` and is never committed.

## Tests

```bash
python -m pip install -e '.[test]'
pytest -q
# With Java and the optional pyspark installation:
python -m pip install -e '.[spark,test]'
pytest -q tests/test_spark.py
PYTHONPATH=src python examples/benchmark.py --observations 20000
```

The local suite includes source-system preflight, dependency-closure tests, snapshot reconciliation and export-manifest inspection. Core tests include bad JSON and duplicate properties, bundle-scoped URNs, source key collisions, orphan references, chronology and date parsing, unmappable legacy data, privacy output permissions and end-to-end reconciliation. Spark tests are an **optional manual CI workflow**, kept separate to avoid spending unnecessary GitHub Actions minutes.

For architecture, patient identity and replay considerations, HL7 profile-vs-quality separation and practical Databricks deployment, see [Migration architecture](docs/migration-architecture.md).

## Security and clinical-use boundaries

- Never commit patient data, a copy of the reference index or `FHIR_MIGRATION_KEY`. Keep secrets in a managed vault. Source, accepted and quarantine payloads may contain special-category health data; apply GDPR, local medical secrecy, retention, encryption, RBAC and access logging as required by the deployment.
- Do not put resource payloads or patient identifiers into monitoring labels, issue logs, exceptions or public benchmark output. The library's issue messages only name fixed rule codes and paths; **caller logging is a separate responsibility**.
- Source logical IDs, ages, diagnosis codes, and even linkable hashes can be personal data. The SQLite index exists temporarily on the local filesystem and contains IDs and patient birthdates; use encrypted, access-controlled scratch storage in healthcare environments.
- National profiles, terminology licensing (e.g., SNOMED CT), clinical logic, patient matching, consent, de-identification and operational acceptance are **outside** this prototype's scope.
- Security reports should go privately to repository maintainers. See `SECURITY.md`.

## Standards and further reading

- [HL7 FHIR R4 4.0.1](https://hl7.org/fhir/R4/), [StructureDefinition](https://hl7.org/fhir/R4/structuredefinition.html), [OperationOutcome](https://hl7.org/fhir/R4/operationoutcome.html)
- [FHIR validation overview](https://hl7.org/fhir/validation.html) and [official Java validator](https://hl7.org/fhir/validator/)
- [FHIR NDJSON](https://hl7.org/fhir/R4/nd-json.html) and [FHIR Bulk Data Access IG](https://hl7.org/fhir/uv/bulkdata/)
- [HL7 Terminology](https://terminology.hl7.org/) and [UCUM](https://ucum.org/)

FHIR is a registered trademark of HL7 International. This independent tool is **not** endorsed by, affiliated with, or certified by HL7. No HL7 icons or trademarks are used as project logos. MIT-licensed code.