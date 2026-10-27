#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
# Synthetic example only! Use a securely stored stable key in real migrations.
export FHIR_MIGRATION_KEY='synthetic-demo-key-not-for-clinical-data'
RUN_DIR="$(mktemp -d)"
chmod 700 "$RUN_DIR"
python -m fhir_quality migrate-csv --input examples/legacy --output "$RUN_DIR/migrated.ndjson" --namespace synthetic
python -m fhir_quality validate --input "$RUN_DIR/migrated.ndjson" --output "$RUN_DIR/gated" --allowlist examples/allowlist.json
python -m fhir_quality inventory create --output "$RUN_DIR/gated" >/dev/null
python -m fhir_quality inventory verify --output "$RUN_DIR/gated"
printf '\nSynthetic validation report:\n' 
cat "$RUN_DIR/gated/summary.json"
printf '\nData written in %s (remove when finished).\n' "$RUN_DIR"
