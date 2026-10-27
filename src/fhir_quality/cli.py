"""fhir-quality entry point. Writes only to local filesystem."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .bulk import validate_ndjson
from .migrate import MigrationConfig, convert_csv
from .rules import Policy
from .readiness import ReadinessThresholds, assess_readiness
from .legacy import profile_legacy
from .inventory import write_inventory, verify_inventory
from .reconcile import compare_snapshots
from .export import inspect_downloaded_export
import os


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fhir-quality", description="FHIR R4 migration quality, not a certified HL7 validator")
    subs = parser.add_subparsers(dest="command", required=True)
    validate = subs.add_parser("validate", help="Three-pass validation and dependency-closed quarantine of NDJSON and Bundles")
    validate.add_argument("--input", required=True, nargs="+", help="One or more NDJSON files (optionally .gz)")
    validate.add_argument("--output", required=True, help="Fresh local destination directory")
    validate.add_argument("--reference-scope", choices=["closed", "open"], default="closed")
    validate.add_argument("--trusted-reference-base", action="append", default=[], metavar="HTTPS_URL",
                          help="Treat absolute resource references under this exact FHIR service root as local")
    validate.add_argument("--complete-resource-types", nargs="*", default=[], metavar="TYPE",
                          help="With open scope, treat missing references to these fully extracted types as errors")
    validate.add_argument("--allowlist", help="JSON dict: coding system to allowed code strings")
    validate.add_argument("--fail-on-quarantine", action="store_true")
    validate.add_argument("--fail-on-readiness", action="store_true", help="Exit 2 when the release readiness gate fails")
    validate.add_argument("--min-pass-rate", type=float, default=0.99)
    preflight = subs.add_parser("profile-legacy", help="Audit legacy CSV keys, relationships and source readiness")
    preflight.add_argument("--input", required=True, help="Directory of legacy CSV files")
    preflight.add_argument("--column-map", help="JSON per-table canonical->source column mapping")
    preflight.add_argument("--fail-on-issues", action="store_true")
    migrate = subs.add_parser("migrate-csv", help="Convert legacy CSV into deterministic FHIR R4 NDJSON")
    migrate.add_argument("--input", required=True, help="Directory with patients/encounters/observations CSV")
    migrate.add_argument("--output", required=True, help="New NDJSON file; won't overwrite")
    migrate.add_argument("--namespace", default="local-demo")
    migrate.add_argument("--identifier-system", default=None, help="Opt-in PHI-bearing source identifiers")
    migrate.add_argument("--column-map", help="JSON object with per-table canonical->source column mappings")
    inventory = subs.add_parser("inventory", help="Create or verify a local accepted-file integrity inventory")
    inventory.add_argument("action", choices=("create", "verify"))
    inventory.add_argument("--output", required=True, help="Existing validate output directory")
    compare = subs.add_parser("compare", help="Compare two NDJSON snapshots without exposing patient IDs")
    compare.add_argument("--previous", nargs="+", required=True, help="Previous NDJSON file(s)")
    compare.add_argument("--current", nargs="+", required=True, help="Current NDJSON file(s)")
    compare.add_argument("--complete-snapshots", action="store_true", help="Only if both inputs have identical complete extraction scope")
    compare.add_argument("--ignore-last-updated", action="store_true")
    compare.add_argument("--fail-on-changes", action="store_true", help="Exit 2 on any additions, updates or absent records")
    export = subs.add_parser("inspect-export", help="Inspect already downloaded HL7 Bulk Data export files, offline")
    export.add_argument("--manifest", required=True, help="HL7 Bulk Data export manifest JSON")
    export.add_argument("--downloads", required=True, help="Flat local directory of previously downloaded files")
    args = parser.parse_args(argv)
    if args.command == "inventory":
        result = write_inventory(args.output) if args.action == "create" else verify_inventory(args.output)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2 if args.action == "verify" and not result["verified"] else 0
    if args.command == "compare":
        key = os.environ.get("FHIR_RECONCILE_KEY", "").encode("utf-8")
        if len(key) < 16:
            parser.error("Set FHIR_RECONCILE_KEY to a stable secret of at least 16 bytes")
        result = compare_snapshots(args.previous, args.current, secret=key,
                                   ignore_meta_last_updated=args.ignore_last_updated,
                                   complete_snapshots=args.complete_snapshots)
        print(json.dumps(result, indent=2, sort_keys=True))
        changed = result["totals"]["added"] or result["totals"]["modified"] or result["totals"].get("deleted", result["totals"].get("unobserved", 0))
        return 2 if not result["safe_for_change_accounting"] or (args.fail_on_changes and changed) else 0
    if args.command == "inspect-export":
        print(json.dumps(inspect_downloaded_export(args.manifest, args.downloads), indent=2, sort_keys=True))
        return 0
    if args.command == "profile-legacy":
        aliases = json.loads(Path(args.column_map).read_text(encoding="utf-8")) if args.column_map else None
        report = profile_legacy(args.input, column_map=aliases)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2 if args.fail_on_issues and not report["preflight_ready"] else 0
    if args.command == "migrate-csv":
        cfg = MigrationConfig.from_env(namespace=args.namespace)
        if args.identifier_system:
            cfg = MigrationConfig(cfg.secret, cfg.namespace, args.identifier_system)
        mappings = json.loads(Path(args.column_map).read_text(encoding="utf-8")) if args.column_map else None
        print(json.dumps(convert_csv(args.input, args.output, cfg, column_map=mappings), indent=2))
        return 0
    codes = {}
    if args.allowlist:
        data = json.loads(Path(args.allowlist).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, list) and all(isinstance(c, str) for c in v) for k, v in data.items()):
            parser.error("allowlist must be an object of string arrays")
        codes = {k: frozenset(v) for k, v in data.items()}
    policy = Policy(reference_scope=args.reference_scope, complete_resource_types=frozenset(args.complete_resource_types), trusted_reference_bases=tuple(args.trusted_reference_base), allowed_observation_codes=codes)
    result = validate_ndjson(args.input, args.output, policy)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    readiness = assess_readiness(result, ReadinessThresholds(min_pass_rate=args.min_pass_rate))
    return 2 if ((args.fail_on_quarantine and result.quarantined) or (args.fail_on_readiness and not readiness["ready"])) else 0


if __name__ == "__main__":
    raise SystemExit(main())
