# Contributing

Contributions are welcome, especially reproducible data-quality bugs and
performance improvements on realistic **synthetic** data.

Before opening a PR, identify the applicable HL7 FHIR release and exact
rule scope, add a failing regression test, implement the smallest robust fix,
and run `pytest -q`. Never commit real patient data. Prefer native Spark SQL
operators over Python UDFs for distributed transforms, and avoid `collect()` on
clinical data.

Name new rules `FHIR_*` only when narrowly grounded in actual R4 structural
requirements; use `DQ_*` for configurable migration/business policies.
Separate official FHIR profile validation from heuristic quality checks.

Keep dependencies optional, maintain Python 3.10 compatibility, and do not
make outbound calls in default validation paths.
