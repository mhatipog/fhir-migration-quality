# Public-preview scope and release criteria

**Status (27 October 2026):** Public, pre-1.0 reference implementation for **offline FHIR R4 migration quality**, not an HL7 conformance validator or a medically approved ETL platform. No PyPI publication is claimed. The MIT-licensed repository can be cloned and evaluated today using fabricated records.

## Practical first use

Run `bash examples/run_demo.sh` on a non-production machine. It takes fabricated legacy CSV data through deterministic mapping, a configurable FHIR quality gate and inventory verification. The example deliberately includes referential mistakes so that the readiness decision is inspectable. It does **not** load a FHIR server or contact outside services.

A reasonable adopter has a legacy EHR/LIS CSV export or existing type-oriented NDJSON files, wants to detect duplicate resource IDs, broken patient/encounter relationships, rejected-reference cascades or silent resource loss between snapshots, and can run an offline Python job in a restricted environment. For Spark teams, the optional module returns lazy accepted/quarantine/issues DataFrames to be written by the caller to governed paths.

## Evidence available now

| Component | What is demonstrated | What is not demonstrated |
| --- | --- | --- |
| Python CLI + migration sample | Self-contained sample, aggregate readiness, quarantine, inventory, and reference checks | Clinical mapping equivalence or a live hospital integration |
| Official MIMIC-IV-on-FHIR demo v2.1.0 (full) | 30 checksum-verified demo files, 928,935 resources, three end-to-end trials, median 126.07 s / 7,368 resources/s on GitHub-hosted Linux | HL7 validator conformance; regulated patient data throughput; clinical acceptance |
| Spark integration | Native SQL DataFrame transformations and five Spark integration tests on local-mode Spark, with small synthetic benchmark measurements | Wide/big-data scalability, Databricks/Delta deployment, full parity with the Python validator |
| Spark on official demo core | Separate opt-in [Spark real-data workflow](../.github/workflows/spark-mimic-benchmark.yml) | Do not claim a measured result until a successful run publishes aggregate evidence |

The Spark validator checks **fewer rules** than the Python validator. Never compare their acceptance totals as though they implement identical clinical or FHIR rules. The subset/open-reference policy can turn missing targets into *warnings*, not proof those targets exist.

## What a real adopter can evaluate

1. Produce an immutable, access-controlled **non-production** export. Confirm the scope and source counts; never upload patient data to public GitHub CI.
2. Run source preflight and an offline FHIR quality gate. Track source-to-output counts, rejected rows, duplicates, unmatched links, and explicitly unverified historical references.
3. Confirm that `accepted + quarantined = parsed input` and that inventory verification detects any change before handoff. Investigate and replay quarantined records from source.
4. Separately validate **FHIR R4 4.0.1 profiles and terminology** with the [HL7 validator](https://hl7.org/fhir/validator/) and your target's Implementation Guides. This toolkit does not do that.
5. Obtain clinical sign-off on identity mapping, units, terminology, dates and history; obtain security/privacy approval for storage, retention, encryption and access. Use a governed target-specific import mechanism.

## Release decision

**Reasonable to publish and invite evaluation:** Yes, as a transparent **pre-1.0 open-source preview** with fully explicit boundaries, reproducible checks and a non-PHI demo. There is no need to wait for a hospital to test the basic offline CLI.

**Not responsible to claim production clinical readiness:** No. Before endorsing clinical deployment, require at least an independent pilot on an authorized realistic extract, explicit R4/IG validator comparison, a documented data-protection review, deterministic rerun/reconciliation evidence, and measured performance plus restart behavior on the actual target infrastructure. For the Spark path, add skew/scale and governed Delta output tests.

**Next modest milestone:** Publish the measured real-data Spark evidence, tidy the benchmark comparison without conflating Spark with the local Python engine, then invite external users to trial the non-production preflight workflow. Treat their first reproducible issue as more valuable than adding many unsupported FHIR resource rules.
