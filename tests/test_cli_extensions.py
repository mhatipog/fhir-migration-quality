import json
import os
from fhir_quality.cli import main


def test_inventory_cli(tmp_path, capsys):
    accepted=tmp_path/'accepted';accepted.mkdir()
    (accepted/'Patient.ndjson').write_text('{"resourceType":"Patient","id":"a"}\n')
    assert main(['inventory','create','--output',str(tmp_path)])==0
    capsys.readouterr()
    assert main(['inventory','verify','--output',str(tmp_path)])==0
    assert json.loads(capsys.readouterr().out)['verified']
    (accepted/'Patient.ndjson').write_text('{"resourceType":"Patient","id":"b"}\n')
    assert main(['inventory','verify','--output',str(tmp_path)])==2


def test_compare_cli(tmp_path, monkeypatch, capsys):
    a=tmp_path/'a.ndjson';b=tmp_path/'b.ndjson'
    a.write_text('{"resourceType":"Patient","id":"a"}\n')
    b.write_text('{"resourceType":"Patient","id":"b"}\n')
    monkeypatch.setenv('FHIR_RECONCILE_KEY','local-synthetic-key-at-least-16')
    assert main(['compare','--previous',str(a),'--current',str(b)])==0
    assert json.loads(capsys.readouterr().out)['totals']['unobserved']==1
    assert main(['compare','--previous',str(a),'--current',str(b),'--fail-on-changes'])==2


def test_cli_compare_missing_key_exits(tmp_path, monkeypatch):
    import pytest
    monkeypatch.delenv('FHIR_RECONCILE_KEY',raising=False)
    with pytest.raises(SystemExit) as e:
        main(['compare','--previous','a','--current','b'])
    assert e.value.code==2
