"""Check FinanceAgent source hashes, local case isolation, and as-of dates."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def checksum(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def validate_materials(data_root):
    root = data_root / 'finance_agent_materials'
    errors, checks = [], {}
    manifest = json.loads((root / 'manifest.json').read_text())
    coverage = json.loads((root / 'coverage_report.json').read_text())
    requirements = json.loads((root / 'requirements.json').read_text())['benchmarks']
    inventory = {r['path']: r for r in manifest['files']}
    hashes = {}
    for relative, record in inventory.items():
        path = data_root / relative
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()) or not path.is_file():
            errors.append('missing or unsafe material: ' + relative)
            continue
        hashes[relative] = checksum(path)
        if hashes[relative] != record['sha256'] or path.stat().st_size != record['bytes']:
            errors.append('material hash/size mismatch: ' + relative)
        parent = record.get('derived_from')
        if parent and (parent not in inventory or record.get('source_sha256') != inventory[parent]['sha256']):
            errors.append('derived material source mismatch: ' + relative)
    for benchmark, scope in coverage['benchmarks'].items():
        original = {r['task_id']: r for r in read_rows(data_root / benchmark / 'tasks.jsonl')}
        cases = read_rows(data_root / benchmark / 'tasks.local.jsonl')
        material_rows = read_rows(data_root / benchmark / 'materials.jsonl')
        material_lookup = {r['task_id']: r for r in material_rows}
        planned = {r['task_id']: r for r in requirements[benchmark]}
        if set(original) != {r['task_id'] for r in cases} or len(cases) != len(original):
            errors.append('local task identities mismatch: ' + benchmark)
        file_count = 0
        for task in cases:
            identity = task['task_id']
            label = benchmark + '/' + identity
            if task['as_of'] != planned[identity]['as_of']:
                errors.append('local task date differs from requirements: ' + label)
            recorded = [r['selector'] for r in material_lookup[identity]['requirements']]
            if recorded != planned[identity]['requirements']:
                errors.append('material selectors differ from requirements: ' + label)
            if task['objective'] != original[identity]['objective']:
                errors.append('local objective differs from official task: ' + label)
            if {'answer', 'gold', 'rubric', 'Rubric', 'eval_input', 'checklist'} & set(task):
                errors.append('reference leakage in local case: ' + label)
            if not task['input_files'] or task['material_status'] != 'downloaded':
                errors.append('local task materials incomplete: ' + label)
            bundle = data_root / task['material_directory']
            if not bundle.resolve().is_relative_to((data_root / benchmark / 'materials').resolve()):
                errors.append('unsafe bundle directory: ' + label)
                continue
            index = json.loads((bundle / 'index.json').read_text())
            if index['task_id'] != identity or index['as_of'] != task['as_of']:
                errors.append('bundle identity/date mismatch: ' + label)
            indexed_paths = set()
            for entry in index['files']:
                relative = entry['path']
                if not relative.startswith('finance_agent_materials/assets/') or relative not in hashes:
                    errors.append('non-asset exposed in bundle: ' + label + ': ' + relative)
                    continue
                target = bundle / entry['bundle_path']
                if not target.is_file() or target.resolve() != (data_root / relative).resolve():
                    errors.append('bundle link missing or incorrect: ' + label + ': ' + relative)
                if entry['sha256'] != hashes[relative]:
                    errors.append('bundle checksum mismatch: ' + label + ': ' + relative)
                indexed_paths.add(str(target.relative_to(data_root)))
                record = inventory[relative]
                dated = record.get('filing_date') or record.get('published_date')
                if dated and dated > task['as_of']:
                    errors.append('source published after task date: ' + label + ': ' + relative)
                if '/api/' in relative and relative.endswith('.json'):
                    payload = json.loads(target.read_text())
                    if payload.get('as_of') != task['as_of']:
                        errors.append('API snapshot as-of mismatch: ' + label)
                    if 'filings' in payload and any(r['filingDate'] > task['as_of'] for r in payload['filings']):
                        errors.append('future filings in API: ' + label)
                    for concepts in payload.get('facts', {}).values():
                        for concept in concepts.values():
                            for points in concept.get('units', {}).values():
                                if any(v.get('filed', '9999') > task['as_of'] for v in points):
                                    errors.append('future financial facts in API: ' + label)
                if '/prices/' in relative and relative.endswith('.json'):
                    payload = json.loads(target.read_text())
                    if not payload['rows'] or any(r['date'] > task['as_of'] for r in payload['rows']):
                        errors.append('empty or future price observations: ' + label)
                    dates = {r['date'] for r in payload['rows']}
                    for req in material_lookup[identity]['requirements']:
                        selector = req['selector']
                        if selector['kind'] == 'price' and selector['symbol'] == payload['metadata']['symbol']:
                            required = selector.get('required_dates', [selector['start_date'], selector['end_date']])
                            if not set(required) <= dates:
                                errors.append('required price observations missing: ' + label + ': ' + relative)
            indexed_paths.add(str((bundle / 'index.json').relative_to(data_root)))
            if indexed_paths != set(task['input_files']):
                errors.append('task/bundle inventory differs: ' + label)
            exposed = {str(p.relative_to(data_root)) for p in bundle.rglob('*') if p.is_file()}
            if exposed != indexed_paths:
                errors.append('unindexed files exposed in bundle: ' + label)
            if material_lookup[identity]['input_file_count'] != len(indexed_paths):
                errors.append('material count differs: ' + label)
            file_count += len(indexed_paths)
        checks[benchmark] = {'task_count': len(cases), 'input_file_links': file_count,
            'downloaded_task_count': scope['downloaded_task_count'],
            'partial_tasks': scope['partial_tasks'], 'local_tasks': benchmark + '/tasks.local.jsonl'}
    report = {'schema_version': 'eval_finance.finance_agent.material_validation.v1',
        'status': 'passed' if not errors else 'failed', 'source_file_count': len(inventory),
        'source_bytes': sum(r['bytes'] for r in inventory.values()), 'benchmarks': checks,
        'errors': errors,
        'verification': 'checksums, original objectives, bundle isolation, source publication dates, API fact filing dates, price dates; no live model/judge; semantic answerability not certified'}
    (root / 'validation_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-root', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    args = p.parse_args()
    result = validate_materials(args.data_root.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(int(result['status'] != 'passed'))
