"""Integrity inventory for an offline migration handover.

Not a FHIR Bulk Data HTTP export manifest: these are local artifact paths.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from .bulk import MAX_LINE_CHARS
from .rules import TYPE_RE, ID_RE, strict_json_loads

ARTIFACT_SCHEMA_VERSION = 1


def _artifact_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise NotADirectoryError("Expected a validator output directory")
    accepted = root / "accepted"
    if not accepted.is_dir() or accepted.is_symlink():
        raise ValueError("No safe accepted/ directory found")
    files = sorted(accepted.glob("*.ndjson"))
    if not files:
        raise ValueError("No accepted FHIR NDJSON files")
    for path in files:
        if path.is_symlink() or not path.is_file() or not TYPE_RE.fullmatch(path.stem):
            raise ValueError("Invalid accepted artifact")
    return files


def build_inventory(output_dir: str | Path) -> dict[str, Any]:
    """Compute exact byte hashes, sizes and per-type resource counts, offline.

    Hashes of PHI-bearing files can themselves be linkable. Treat inventory as
    sensitive and do not publish it to a public issue or CI build log.
    """
    root = Path(output_dir)
    files = _artifact_files(root)
    report: dict[str, Any] = {"schema_version": ARTIFACT_SCHEMA_VERSION,
                              "kind": "local-fhir-migration-inventory", "files": [], "total_resources": 0}
    for path in files:
        sha = hashlib.sha256()
        count = 0
        with path.open("rb") as f:
            start_stat = os.fstat(f.fileno())
            while True:
                raw = f.readline(MAX_LINE_CHARS + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE_CHARS or not raw.strip():
                    raise ValueError("Malformed or oversized accepted NDJSON line")
                sha.update(raw)
                try:
                    resource = strict_json_loads(raw)
                except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
                    raise ValueError("Accepted artifact has invalid JSON") from None
                if not isinstance(resource, dict) or resource.get("resourceType") != path.stem:
                    raise ValueError("Accepted artifact is not homogeneous FHIR NDJSON")
                rid = resource.get("id")
                if not isinstance(rid, str) or not ID_RE.fullmatch(rid):
                    raise ValueError("Accepted artifact has invalid logical id")
                count += 1
            end_stat = os.fstat(f.fileno())
            if (start_stat.st_size, start_stat.st_mtime_ns) != (end_stat.st_size, end_stat.st_mtime_ns):
                raise ValueError("Accepted artifact changed during inventory")
        report["files"].append({"path": path.relative_to(root).as_posix(),
                                 "sha256": sha.hexdigest(), "bytes": path.stat().st_size,
                                 "resource_type": path.stem, "resources": count})
        report["total_resources"] += count
    return report


def write_inventory(output_dir: str | Path, *, filename: str = "inventory.json") -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", filename):
        raise ValueError("Unsafe inventory file name")
    root = Path(output_dir)
    report = build_inventory(root)
    fd = os.open(root / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    return report


def verify_inventory(output_dir: str | Path, *, filename: str = "inventory.json") -> dict:
    root = Path(output_dir)
    reference = root / filename
    if reference.is_symlink() or not reference.is_file():
        raise ValueError("Inventory does not exist or is a symlink")
    with reference.open(encoding="utf-8") as f:
        expected = json.load(f)
    actual = build_inventory(root)
    return {"verified": expected == actual,
            "files": len(actual["files"]), "resources": actual["total_resources"]}
