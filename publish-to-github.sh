#!/usr/bin/env bash
# Publish only after reviewing the source archive. No PHI or credentials belong in git.
set -euo pipefail
cd "$(dirname "$0")"
REPO="mhatipog/fhir-migration-quality"
command -v gh >/dev/null || { echo 'GitHub CLI required; install gh and authenticate first.' >&2; exit 1; }
gh auth status
if gh repo view "$REPO" >/dev/null 2>&1; then
  echo "Repository already exists ($REPO). Refusing to overwrite or force-push." >&2
  exit 1
fi
if [ ! -d .git ]; then git init -b main; fi
# The repository may be inspected by colleagues. Keep all PHI-bearing data outside git.
if git ls-files --cached --others --exclude-standard | grep -E '(^private/|^outputs/|\.ndjson(\.gz)?$|\.sqlite3$|\.p12$|\.pem$|\.key$)' >/dev/null; then
  echo 'Potential sensitive artifact found. Review and remove it before publishing.' >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  git add .
  git commit -m 'Add FHIR migration quality gates and reproducible benchmarks'
fi
gh repo create "$REPO" --public --source . --remote origin --push \
  --description 'FHIR R4 migration validation, HL7 Bulk Data checks, healthcare ETL and Databricks quarantine'
# Topics make the implementation findable without stuffing keywords into prose.
gh api -X PUT "repos/$REPO/topics" \
  -f 'names[]=hl7-fhir' -f 'names[]=fhir-r4' -f 'names[]=healthcare-data' \
  -f 'names[]=data-migration' -f 'names[]=data-quality' \
  -f 'names[]=pyspark' -f 'names[]=databricks' \
  -f 'names[]=bulk-data' -f 'names[]=python' >/dev/null || \
  echo 'Repo created, but topics could not be set automatically.' >&2
printf 'Repository: https://github.com/%s\n' "$REPO"
