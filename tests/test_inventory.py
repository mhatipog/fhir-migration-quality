import json
import os
from pathlib import Path
import pytest
from fhir_quality.inventory import build_inventory, verify_inventory, write_inventory


def prepared(tmp_path):
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    (accepted / "Patient.ndjson").write_text('{"resourceType":"Patient","id":"p"}\n')
    return tmp_path


def test_create_verify_and_tamper(tmp_path):
    root = prepared(tmp_path)
    report = write_inventory(root)
    assert report["total_resources"] == 1
    assert len(report["files"][0]["sha256"]) == 64
    assert verify_inventory(root)["verified"]
    assert os.stat(root / "inventory.json").st_mode & 0o777 == 0o600
    (root / "accepted" / "Patient.ndjson").write_text('{"resourceType":"Patient","id":"p2"}\n')
    assert not verify_inventory(root)["verified"]


def test_wrong_type_and_symlink_rejected(tmp_path):
    root = prepared(tmp_path)
    (root / "accepted" / "Patient.ndjson").write_text('{"resourceType":"Observation","id":"x"}\n')
    with pytest.raises(ValueError):
        build_inventory(root)
    (root / "accepted" / "Patient.ndjson").unlink()
    foreign = tmp_path / "secret.ndjson"
    foreign.write_text('{"resourceType":"Patient","id":"p"}\n')
    (root / "accepted" / "Patient.ndjson").symlink_to(foreign)
    with pytest.raises(ValueError):
        build_inventory(root)


def test_inventory_does_not_overwrite(tmp_path):
    root = prepared(tmp_path)
    write_inventory(root)
    with pytest.raises(FileExistsError):
        write_inventory(root)


def test_duplicate_json_and_blank_lines_rejected(tmp_path):
    root=prepared(tmp_path)
    p=root/'accepted'/'Patient.ndjson'
    p.write_text('{"resourceType":"Patient","id":"p","id":"q"}\n')
    with pytest.raises(ValueError):
        build_inventory(root)
    p.write_text('{"resourceType":"Patient","id":"p"}\n\n')
    with pytest.raises(ValueError):
        build_inventory(root)
