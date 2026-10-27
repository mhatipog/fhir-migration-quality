# Migrating an EHR extract into FHIR R4 with a quality gate

This is a field guide for data engineers handling offline extracts from an EHR,
LIS, pharmacy system or legacy warehouse. No FHIR server or external terminology
service is required for the **local** checks. Clinical sign-off, local consent,
access-control rules and official profile validation remain outside the scope
of this package.

## 1. Declare the extraction boundary

Start with an inventory of source tables and the FHIR resource types they map
to. Record which data domains are **complete** in this extract. If Patient and
Encounter are complete but Organization and Practitioner are not, do not turn
missing Organization references into fatal errors: doing that would quarantine
valid partial extracts. Conversely, do not silently accept missing Patients.

Create a new restricted working directory. Use source system backups and
versioned mapping specifications; this package never mutates the source system.

```bash
umask 077
mkdir -p private
fhir-quality profile-legacy --input private/source-csv --fail-on-issues
```

Fix missing columns, duplicate source IDs and broken relationships before
conversion. Do not write source identifiers to console logs or commits.

## 2. Convert and validate

Use a stable HMAC secret from your secrets manager. An unstable mapping key
makes re-runs look like unrelated new FHIR records.

```bash
fhir-quality migrate-csv \
  --input private/source-csv \
  --output private/mapped.ndjson \
  --namespace hospital-prod-migration

fhir-quality validate \
  --input private/mapped.ndjson \
  --output private/check-001 \
  --reference-scope open \
  --complete-resource-types Patient Encounter \
  --trusted-reference-base https://fhir.example.org/r4 \
  --fail-on-readiness
```

The command above requires `FHIR_MIGRATION_KEY` to be defined in the process
environment (not in a notebook cell or checked-in `.env`). This is a demo
pipeline; real mapping requires site-specific profile, terminology, unit,
code-system and consent review.

Do not claim "validated against HL7" on this result. You have only passed the
implemented offline quality policy. Run the HL7 Java validator with the correct
IG packages separately.

## 3. Triage quarantined records

The output directory contains:

| File | Use |
| --- | --- |
| `accepted/<ResourceType>.ndjson` | Accepted resources; input to the next stage only after release approval |
| `quarantine.ndjson` | Original quarantined FHIR resource inside an envelope; **PHI-sensitive** |
| `issues.ndjson` | Issue code, severity and JSON Pointer with file/line/Bundle entry; avoid revealing raw values |
| `operation-outcomes.ndjson` | FHIR OperationOutcome representations for interoperability tooling |
| `summary.json` | Summary by issue and type, with release-readiness checks |

`source.file_index` is the 1-based ordinal among the validator's sorted input
paths, `source.line` is the physical line, and `source.bundle_entry` is the
0-based Bundle entry. Record which exact files were passed to the command in a
*private* handover log so you can locate records later. The issue output itself
never needs to contain a source file name or patient identifier.

Fix errors at source or in a versioned transformation rule; rerun into a new
output directory. Never replay quarantined records blindly. In particular,
`DQ_REFERENCE_TARGET_QUARANTINED` may reflect a valid dependency on a Patient
or Encounter that failed an earlier check. Review upstream causes first.

## 4. Know what "ready" means

- A missing reference to a **declared complete type** is a hard error even when
  other types use open-world reference policy.
- A reference to a resource already quarantined is a hard error, propagating
  through the local dependency graph, including cycles.
- A version-specific reference such as `Patient/x/_history/2` cannot be checked
  by a latest-ID-only export. It remains a warning and **blocks release
  readiness** until resolved with a version-aware store.
- Missing records from undeclared partial extracts are warnings. A warning-free
  report is not a guarantee of completeness.
- A passing quality gate is not an official FHIR conformance result, clinical
  validity assertion, or authorization to publish patient data.

## 5. Verify the handoff and reruns

Create the integrity inventory only after reviewing quarantine and summary.
Before loading into the target system, re-check it:

```bash
fhir-quality inventory create --output private/check-001
fhir-quality inventory verify --output private/check-001
```

For incremental migrations, compare the accepted snapshot with a prior
approved one. Use a separate stable reconciliation key and **do not claim
missing rows are deletions** unless extraction scope is provably complete.

```bash
fhir-quality compare \
  --previous private/prior/accepted/*.ndjson \
  --current private/check-001/accepted/*.ndjson
```

## 6. Databricks and Spark boundary

The optional `fhir_quality.spark` helpers perform a native DataFrame subset of
the checks, using joins/expressions and a quarantine DataFrame. They **do not**
implement the full offline graph, arbitrary nested reference traversal, HMAC
identity mapping or official IG validation. Use them for Bronze-to-Silver
quality gates, then add other validation stages before final release.

For regulated clinical systems, the source, checkpoint, Delta logs and rejected
records all require the same access and retention controls. Restrict notebooks
and query histories; do not emit patient-level values in exceptions, metrics,
issue trackers or CI artifacts. Decide which storage locations are allowed for
PHI *before* running any example on hospital data.

## 7. Run the public clinical compatibility workload separately

The open [MIMIC-IV Clinical Database Demo on FHIR v2.1.0](https://physionet.org/content/mimic-iv-fhir-demo/2.1.0/)
provides de-identified real clinical encounters for interoperability testing.
It is different from the credentialed full MIMIC-IV FHIR dataset. Follow its
ODbL license and data-use terms. Download with `benchmarks/download_mimic.py`,
which checks published SHA-256 hashes. `benchmarks/bench.py` writes **only
aggregate counts, timings and whole-input hashes**. Review even aggregate
results under local disclosure policy before publishing.

The synthetic benchmarks in this repository are for deterministic regression
measurements and include no clinical records. They are not presented as a
substitute for benchmarking against real hospital exports.
