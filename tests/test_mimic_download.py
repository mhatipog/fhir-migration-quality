"""Offline downloader regressions: resumability, integrity, and size limits."""
from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path

import pytest

# Bench scripts are intentionally not installed as a Python package.
SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks" / "download_mimic.py"
spec = importlib.util.spec_from_file_location("download_mimic", SCRIPT)
assert spec is not None and spec.loader is not None
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)


class FakeResponse:
    def __init__(self, data: bytes, *, status: int = 200, headers=None,
                 fail_after: int | None = None, url: str = dl.BASE + "fhir/x.ndjson.gz"):
        self.body = io.BytesIO(data)
        self.status = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}
        self.fail_after = fail_after
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        if self.fail_after is not None and self.body.tell() >= self.fail_after:
            raise TimeoutError("injected broken connection")
        cap = size if self.fail_after is None else min(size, self.fail_after - self.body.tell())
        return self.body.read(cap)


def test_resume_after_timeout_with_range(tmp_path, monkeypatch):
    blob = b"0123456789abcdef" * 200
    seen = []

    def open_request(request, timeout):
        offset = request.headers.get("Range")
        seen.append(offset)
        if not offset:
            return FakeResponse(blob, fail_after=1024)
        assert offset == "bytes=1024-"
        return FakeResponse(blob[1024:], status=206,
                            headers={"Content-Range": f"bytes 1024-{len(blob)-1}/{len(blob)}",
                                     "Content-Length": str(len(blob)-1024)})

    monkeypatch.setattr(dl, "urlopen", open_request)
    monkeypatch.setattr(dl.time, "sleep", lambda _: None)
    monkeypatch.setattr(dl, "CHUNK_SIZE", 1024)
    result = dl.download_verified("fhir/x.ndjson.gz", hashlib.sha256(blob).hexdigest(),
                                  tmp_path, attempts=3)
    assert result == "verified"
    assert seen == [None, "bytes=1024-"]
    assert (tmp_path / "x.ndjson.gz").read_bytes() == blob
    assert not (tmp_path / "x.ndjson.gz.part").exists()


def test_ignored_range_restarts_cleanly(tmp_path, monkeypatch):
    blob = b"correct-payload" * 20
    (tmp_path / "x.ndjson.gz.part").write_bytes(blob[:15])
    seen = []

    def open_request(request, timeout):
        seen.append(request.headers.get("Range"))
        return FakeResponse(blob)  # Ignores Range and returns 200

    monkeypatch.setattr(dl, "urlopen", open_request)
    assert dl.download_verified("fhir/x.ndjson.gz", hashlib.sha256(blob).hexdigest(), tmp_path) == "verified"
    assert seen == ["bytes=15-"]
    assert (tmp_path / "x.ndjson.gz").read_bytes() == blob


def test_wrong_resume_range_must_not_corrupt_file(tmp_path, monkeypatch):
    part = tmp_path / "x.ndjson.gz.part"
    part.write_bytes(b"hello")

    def wrong_range(*_args, **_kwargs):
        return FakeResponse(b"bytes", status=206,
                            headers={"Content-Range": "bytes 0-4/10"})

    monkeypatch.setattr(dl, "urlopen", wrong_range)
    with pytest.raises(ValueError, match="Content-Range"):
        dl.download_verified("fhir/x.ndjson.gz", "0" * 64, tmp_path)
    assert part.read_bytes() == b"hello"
    assert not (tmp_path / "x.ndjson.gz").exists()


def test_bad_checksum_never_promoted(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "urlopen", lambda *_a, **_kw: FakeResponse(b"invalid"))
    monkeypatch.setattr(dl.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="Download failed"):
        # No invalid bytes may be promoted, even across retries.
        dl.download_verified("fhir/x.ndjson.gz", hashlib.sha256(b"expected").hexdigest(), tmp_path, attempts=2)
    assert not (tmp_path / "x.ndjson.gz").exists()


def test_verified_existing_file_skips_download(tmp_path, monkeypatch):
    blob = b"correct"
    (tmp_path / "x.ndjson.gz").write_bytes(blob)
    monkeypatch.setattr(dl, "urlopen", lambda *_a, **_kw: pytest.fail("unexpected HTTP request"))
    assert dl.download_verified("fhir/x.ndjson.gz", hashlib.sha256(blob).hexdigest(), tmp_path) == "cached"


def test_corrupted_existing_file_rejected(tmp_path, monkeypatch):
    (tmp_path / "x.ndjson.gz").write_bytes(b"invalid")
    monkeypatch.setattr(dl, "urlopen", lambda *_a, **_kw: pytest.fail("unexpected HTTP request"))
    with pytest.raises(ValueError, match="checksum"):
        dl.download_verified("fhir/x.ndjson.gz", "0" * 64, tmp_path)


def test_enforces_size_limit_even_without_content_length(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "urlopen", lambda *_a, **_kw: FakeResponse(b"123456789", headers={}))
    with pytest.raises(ValueError, match="size limit"):
        dl.download_verified("fhir/x.ndjson.gz", "0" * 64, tmp_path, max_bytes=8)
    assert not (tmp_path / "x.ndjson.gz").exists()


def test_host_redirect_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "urlopen", lambda *_a, **_kw: FakeResponse(b"ok", url="https://other.example/file"))
    with pytest.raises(ValueError, match="redirected"):
        dl.download_verified("fhir/x.ndjson.gz", "0" * 64, tmp_path)
    assert not (tmp_path / "x.ndjson.gz").exists()


def test_existing_partial_is_resumed_across_reinvocations(tmp_path, monkeypatch):
    blob = b"fixed" * 101
    part = tmp_path / "x.ndjson.gz.part"
    part.write_bytes(blob[:100])

    def open_request(request, timeout):
        assert request.headers.get("Range") == "bytes=100-"
        return FakeResponse(blob[100:], status=206,
                            headers={"Content-Range": f"bytes 100-{len(blob)-1}/{len(blob)}"})

    monkeypatch.setattr(dl, "urlopen", open_request)
    assert dl.download_verified("fhir/x.ndjson.gz", hashlib.sha256(blob).hexdigest(), tmp_path) == "verified"
    assert (tmp_path / "x.ndjson.gz").read_bytes() == blob


def test_parse_official_checksums_format():
    entries = dl.parse_checksums("a" * 64 + "  fhir/MimicPatient.ndjson.gz\n")
    assert entries == {"fhir/MimicPatient.ndjson.gz": "a" * 64}
    assert dl.parse_checksums("0" * 64 + " ../untrusted/file.ndjson.gz") == {}