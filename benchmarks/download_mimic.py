"""Download the public MIMIC-IV-on-FHIR demo with checksums and safe resume.

Only the official versioned PhysioNet URL is used. Incomplete files are kept
locally as .part; a file is promoted to its final name only after SHA-256
verification. Raw patient data must not be uploaded to GitHub artifacts.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import os
import re
import stat
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BASE = "https://physionet.org/files/mimic-iv-fhir-demo/2.1.0/"
CORE = ("MimicPatient", "MimicEncounter", "MimicEncounterED", "MimicEncounterICU",
        "MimicCondition", "MimicObservationED", "MimicObservationLabevents")
MAX_SIZE = 2 * 1024**3  # Per compressed file, not the extraction size.
CHUNK_SIZE = 1024 * 1024
HOSTS = {"physionet.org", "www.physionet.org"}
RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)\Z")


def _check_response_host(response) -> None:
    parsed = urlsplit(response.geturl())
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Download redirected outside the approved PhysioNet origin")


def read_bounded(url: str, max_bytes: int = 1024 * 1024) -> bytes:
    """Read only the small checksum manifest into memory."""
    with urlopen(Request(url, headers={"Accept-Encoding": "identity"}), timeout=60) as response:
        _check_response_host(response)
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("Checksum manifest exceeded size limit")
        return payload


def parse_checksums(data: str) -> dict[str, str]:
    result = {}
    for line in data.splitlines():
        found = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(?:\./)?(fhir/[\w.-]+\.ndjson\.gz)", line.strip())
        if found:
            result[found.group(2)] = found.group(1).lower()
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _part_file(path: Path):
    """Open an existing regular .part file without following symlinks."""
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Partial download is not a regular file")
    return os.fdopen(fd, "r+b")


def _transfer(url: str, part: Path, max_bytes: int, timeout: float) -> int:
    with _part_file(part) as sink:
        sink.seek(0, os.SEEK_END)
        offset = sink.tell()
        if offset > max_bytes:
            raise ValueError("Partial download exceeded size limit")
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            _check_response_host(response)
            status = response.status
            expected_bytes = None
            if offset and status == 206:
                match = RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if not match or int(match.group(1)) != offset:
                    raise ValueError("Server returned an unexpected Content-Range")
                end = int(match.group(2))
                total = match.group(3)
                if end < offset or (total != "*" and int(total) <= end):
                    raise ValueError("Server returned an invalid Content-Range")
                expected_bytes = end - offset + 1
            elif status == 200:
                # Some CDNs ignore Range. Re-download rather than append
                # duplicate bytes and rely on SHA-256 to detect corruption.
                sink.seek(0)
                sink.truncate(0)
                offset = 0
            else:
                raise ValueError(f"Unexpected HTTP status {status} for download")

            length = response.headers.get("Content-Length")
            if length is not None:
                if not length.isdecimal():
                    raise ValueError("Invalid Content-Length")
                announced = int(length)
                if expected_bytes is not None and announced != expected_bytes:
                    raise ValueError("Range length disagrees with Content-Length")
                expected_bytes = announced
                if offset + announced > max_bytes:
                    raise ValueError("File exceeds configured compressed size limit")

            transferred = 0
            while True:
                block = response.read(CHUNK_SIZE)
                if not block:
                    break
                transferred += len(block)
                if offset + transferred > max_bytes:
                    raise ValueError("File exceeded configured compressed size limit")
                if expected_bytes is not None and transferred > expected_bytes:
                    raise ValueError("Server sent more data than advertised")
                sink.write(block)
            sink.flush()
            if expected_bytes is not None and transferred != expected_bytes:
                raise OSError("Transfer ended before all advertised bytes arrived")
        return offset + transferred


def download_verified(name: str, checksum: str, destination: Path,
                      *, attempts: int = 10, timeout: float = 90,
                      max_bytes: int = MAX_SIZE) -> str:
    """Return 'verified' or 'cached'; never accept a mismatched final file."""
    if not re.fullmatch(r"fhir/[\w.-]+\.ndjson\.gz", name):
        raise ValueError("Unexpected dataset filename")
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Invalid expected checksum")
    if not 1 <= attempts <= 30 or timeout <= 0 or max_bytes <= 0:
        raise ValueError("Invalid download limits")
    final = destination / Path(name).name
    part = destination / (Path(name).name + ".part")
    if final.is_symlink() or part.is_symlink():
        raise ValueError("Refusing symlink for downloaded data")
    if final.exists():
        if not final.is_file() or final.stat().st_size > max_bytes or sha256_file(final) != checksum:
            raise ValueError(f"Existing file failed checksum: {final.name}")
        print(f"Already verified: {final.name} ({final.stat().st_size} bytes)", flush=True)
        return "cached"

    if part.exists() and part.is_file() and part.stat().st_size <= max_bytes and sha256_file(part) == checksum:
        os.link(part, final, follow_symlinks=False)
        part.unlink()
        print(f"Verified existing partial: {final.name} ({final.stat().st_size} bytes)", flush=True)
        return "verified"

    last_error: Exception | None = None
    for number in range(1, attempts + 1):
        try:
            _transfer(BASE + name, part, max_bytes, timeout)
            if sha256_file(part) == checksum:
                # Hard-link creation cannot overwrite a pre-existing target.
                # Keep patient payloads out of the stdout and artifacts.
                os.link(part, final, follow_symlinks=False)
                part.unlink()
                print(f"Verified: {final.name} ({final.stat().st_size} compressed bytes)", flush=True)
                return "verified"
            # A successful HTTP response with incomplete or corrupt data is
            # not sufficient for promotion. Retry once from byte zero.
            last_error = ValueError("Checksum mismatch or truncated response")
            with _part_file(part) as sink:
                sink.truncate(0)
        except (TimeoutError, ConnectionError, OSError, HTTPError, URLError) as exc:
            last_error = exc
            # Checksum-sensitive partial bytes are preserved for resume.
        if number < attempts:
            delay = min(2 ** min(number - 1, 5), 30)
            print(f"Retry {number}/{attempts - 1}: {Path(name).name} ({type(last_error).__name__}); "
                  f"waiting {delay}s", flush=True)
            time.sleep(delay)
    raise RuntimeError(f"Download failed after {attempts} attempts: {Path(name).name} "
                       f"({type(last_error).__name__}); partial file retained") from last_error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, help="Local private directory (can contain previous downloads)")
    parser.add_argument("--core", action="store_true", help="Seven common types; not a complete dataset")
    parser.add_argument("--attempts", type=int, default=10, help="HTTP attempts per file (default: 10)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel file transfers (1-3; default: 1)")
    parser.add_argument("--timeout", type=float, default=90, help="Socket read timeout in seconds")
    parser.add_argument("--max-file-gib", type=int, default=2, help="Maximum compressed file size in GiB")
    args = parser.parse_args(argv)
    dest = Path(args.destination)
    if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
        parser.error("Destination must be a regular directory, not a symlink")
    checks = parse_checksums(read_bounded(BASE + "SHA256SUMS.txt").decode("utf-8"))
    if not checks:
        raise ValueError("No FHIR NDJSON hashes parsed; aborting")
    chosen = [f"fhir/{name}.ndjson.gz" for name in CORE] if args.core else sorted(checks)
    if any(name not in checks for name in chosen):
        raise ValueError("Selected files missing from official checksum list")
    if not 1 <= args.attempts <= 30 or args.max_file_gib <= 0 or not 1 <= args.workers <= 3:
        parser.error("Invalid attempts, workers or file size limit")
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    def fetch(name: str) -> str:
        return download_verified(name, checks[name], dest, attempts=args.attempts,
                                 timeout=args.timeout, max_bytes=args.max_file_gib * 1024**3)

    if args.workers == 1:
        for name in chosen:
            fetch(name)
    else:
        # Each worker writes its own uniquely named .part file. Files are never
        # promoted until their individual PhysioNet checksum has been verified.
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(fetch, name): name for name in chosen}
            try:
                for completed in as_completed(futures):
                    completed.result()
            except BaseException:
                for pending in futures:
                    pending.cancel()
                raise
    print(f"All {len(chosen)} files checksum-verified. No patient records printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
