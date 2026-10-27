"""Deterministic synthetic input for repeatable local performance regression checks."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def generate(path: Path, n: int, rich=False, invalid_patient_every: int = 0):
    if not 1 <= n <= 1_000_000 or path.exists() or invalid_patient_every < 0:
        raise ValueError('n out of range or output exists')
    with path.open('x', encoding='utf-8') as handle:
        np = max(n // 10, 1)
        for i in range(np):
            row = {'resourceType':'Patient', 'id':f'p{i}',
                   'birthDate':'1970-02-30' if invalid_patient_every and i % invalid_patient_every == 0 else '1970-02-28',
                   'identifier':[{'system':'https://example.org/synthetic','value':f'fake{i}'}]}
            handle.write(json.dumps(row, separators=(',', ':'))+'\n')
        for i in range(n):
            row = {'resourceType':'Observation', 'id':f'o{i}', 'status':'final',
                   'code':{'coding':[{'system':'http://loinc.org', 'code':'15074-8'}]},
                   'subject':{'reference':f'Patient/p{i%np}'},
                   'valueQuantity':{'value':4.9,'unit':'mmol/L','system':'http://unitsofmeasure.org','code':'mmol/L'}}
            if rich:
                row.update({'effectiveDateTime':'2026-01-01T12:33:00Z',
                            'meta':{'profile':['https://example.org/StructureDefinition/locally-synthetic']},
                            'category':[{'coding':[{'system':'http://terminology.hl7.org/CodeSystem/observation-category','code':'laboratory'}]}],
                            'performer':[{'reference':f'Organization/org{i%3}'}],
                            'component':[{'code':{'coding':[{'system':'http://loinc.org','code':'1234-5'}]},'valueQuantity':{'value':5.1}},
                                         {'code':{'coding':[{'system':'http://loinc.org','code':'2345-6'}]},'valueString':'demo'}]})
            handle.write(json.dumps(row, separators=(',', ':'))+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--observations', type=int, default=20000)
    p.add_argument('--rich', action='store_true')
    p.add_argument('--invalid-patient-every', type=int, default=0, help='Deterministic synthetic validation and cascade stress case')
    args=p.parse_args()
    generate(args.output, args.observations, args.rich, args.invalid_patient_every)
