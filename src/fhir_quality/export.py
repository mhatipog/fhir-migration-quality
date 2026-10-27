"""Offline quality inspection of downloaded FHIR Bulk Data export files.

The HL7 Bulk Data IG manifest is a server document containing URLs. We never
fetch those URLs: a trusted export client must authenticate and download first.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit
import json

from .bulk import records_from_ndjson
from .rules import TYPE_RE, _instant, strict_json_loads


def inspect_downloaded_export(manifest: str | Path, download_dir: str | Path) -> dict:
    """Match manifest output entries to files downloaded into a flat local folder.

    Only local files named by the URL's final path component are inspected. Reject
    ambiguous basenames, symlinks, malformed manifests and mixed resource types.
    No network calls, URL requests, credentials or patient identifiers in report.
    """
    root = Path(download_dir)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Download directory must be a real local directory")
    document = strict_json_loads(Path(manifest).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Export manifest must be a JSON object")
    if _instant(document.get("transactionTime")) is None:
        raise ValueError("Export manifest requires a valid transactionTime instant")
    if not isinstance(document.get("output"), list):
        raise ValueError("Export manifest requires an output array")
    files = []
    seen_names = set()
    for item in document["output"]:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) or not TYPE_RE.fullmatch(item["type"]):
            raise ValueError("Export output entry has invalid type")
        uri = item.get("url")
        if not isinstance(uri, str):
            raise ValueError("Export output entry has invalid URL")
        parts = urlsplit(uri)
        if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username or parts.password or parts.fragment:
            raise ValueError("Export URL is not an HTTP(S) location")
        segments = [unquote(x) for x in parts.path.split("/")]
        if any(x in {".", ".."} or "\\" in x or "\0" in x or "/" in x for x in segments):
            raise ValueError("Unsafe export filename")
        basename = segments[-1]
        if not basename or basename in seen_names:
            raise ValueError("Export URLs have empty or ambiguous local filenames")
        seen_names.add(basename)
        path = root / basename
        if path.is_symlink() or not path.is_file() or path.parent != root:
            raise ValueError("A downloaded export file is missing or unsafe")
        files.append((item["type"], path))
    type_counts = Counter()
    count = 0
    for required_type, path in files:
        for rec in records_from_ndjson([path]):
            if rec.parse_error or not isinstance(rec.resource, dict):
                raise ValueError("Export file contains malformed NDJSON")
            if rec.resource.get("resourceType") != required_type:
                raise ValueError("Export output contains mixed or unexpected resource types")
            count += 1
            type_counts[required_type] += 1
    return {
        "manifest_type": "HL7 Bulk Data export (offline inspection)",
        "files": len(files), "resources": count,
        "resources_by_type": dict(sorted(type_counts.items())),
        "manifest_outcome_files": len(document.get("error", [])) if isinstance(document.get("error", []), list) else 0,
        "notes": "This checks downloaded output organization, not auth, remote server correctness or FHIR profiles.",
    }
