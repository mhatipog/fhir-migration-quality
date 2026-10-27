"""Explicit, checksum-verified download of the public MIMIC-IV-on-FHIR demo.

Download is opt-in. Only the official versioned PhysioNet host is contacted.
Downloaded medical data belong outside the repository. Cite the original dataset.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from urllib.request import urlopen

BASE = "https://physionet.org/files/mimic-iv-fhir-demo/2.1.0/"
CORE = ("MimicPatient", "MimicEncounter", "MimicEncounterED", "MimicEncounterICU",
        "MimicCondition", "MimicObservationED", "MimicObservationLabevents")
MAX_SIZE = 100 * 1024 * 1024  # per compressed file


def read_bounded(url: str, max_bytes: int = MAX_SIZE) -> bytes:
    with urlopen(url, timeout=45) as response:
        if response.url.split("/")[2] not in ("physionet.org", "www.physionet.org"):
            raise ValueError("Unexpected download redirect host")
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("Exceeded download size limit")
        return payload


def parse_checksums(data: str) -> dict[str, str]:
    result = {}
    for line in data.splitlines():
        found = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(?:\./)?(fhir/[\w.-]+\.ndjson\.gz)", line.strip())
        if found:
            result[found.group(2)] = found.group(1).lower()
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--destination", required=True, help="New local directory outside tracked source")
    p.add_argument("--core", action="store_true", help="Only seven common resource files; not a complete extraction")
    a = p.parse_args(argv)
    dest = Path(a.destination)
    if dest.exists():
        p.error("Destination exists; refusing to overwrite")
    # No insecure 'download without checksum' fallback.
    checksum_text = read_bounded(BASE + "SHA256SUMS.txt", max_bytes=1024*1024).decode("utf-8")
    checks = parse_checksums(checksum_text)
    if not checks:
        raise ValueError("No FHIR NDJSON gzip hashes parsed; checksum format changed; abort")
    chosen = [f"fhir/{name}.ndjson.gz" for name in CORE] if a.core else sorted(checks)
    if any(name not in checks for name in chosen):
        raise ValueError("Selected files missing from official checksum list")
    dest.mkdir(mode=0o700, parents=True)
    for name in chosen:
        blob = read_bounded(BASE + name)
        if hashlib.sha256(blob).hexdigest() != checks[name]:
            raise ValueError(f"Checksum mismatch: {Path(name).name}")
        fd = os.open(dest / Path(name).name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        print(f"Verified: {Path(name).name} ({len(blob)} compressed bytes)")
    print(f"Downloaded and verified {len(chosen)} official files. No records displayed.")


if __name__ == "__main__":
    sys.exit(main())
