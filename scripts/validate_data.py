"""Validate local native references, input assets, and pinned data checksums."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

# Source-checkout execution, with an installed package also supported.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval_finance.common import normalize_case
from eval_finance.config import load_config
from eval_finance.registry import get_evaluator
from eval_finance.scripts.prepare_data import read_jsonl


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def validate(root):
    sources = json.loads((root / 'sources.json').read_text())
    summary = json.loads((root / 'index_summary.json').read_text())
    expected = {spec['path']: spec for spec in sources['local_snapshots']}
    expected.update({spec['path']: spec for spec in sources['downloads']})
    inventory, errors, checks = [], [], {}
    material_report = None
    material_manifest = root / 'finance_agent_materials/manifest.json'
    if material_manifest.is_file():
        from eval_finance.scripts.validate_finance_agent_materials import validate_materials
        material_report = validate_materials(root)
        errors.extend(material_report['errors'])
        expected.update({spec['path']: spec for spec in json.loads(material_manifest.read_text())['files']})
    for dirname, _, files in os.walk(root):
        for name in sorted(files):
            path = Path(dirname) / name
            relative = str(path.relative_to(root))
            if relative in {'manifest.json', 'validation_report.json'}:
                continue
            with path.open('rb') as stream:
                prefix = stream.read(50)
            if prefix.startswith(b'version https://git-lfs.github.com/spec/v1'):
                errors.append('unresolved Git LFS pointer: ' + relative)
            spec = expected.get(relative, {})
            origin = spec
            archive_origin = {}
            if not origin:
                for archive in sources['archives']:
                    prefix = archive['destination'] + '/'
                    if relative.startswith(prefix):
                        origin = expected[archive['path']]
                        archive_origin = {'source_archive': archive['path'], 'decoded_archive_entry': relative[len(prefix):]}
                        break
            checksum = digest(path)
            if spec.get('sha256') and checksum != spec['sha256']:
                errors.append('SHA-256 mismatch: ' + relative)
            if spec.get('bytes') is not None and path.stat().st_size != spec['bytes']:
                errors.append('size mismatch: ' + relative)
            inventory.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': checksum,
                              **({'source_url': origin['url'], 'source_revision': origin.get('revision')} if origin.get('url') else {}),
                              **({'derived_from': origin['derived_from']} if origin.get('derived_from') else {}),
                              **archive_origin})
    for path, spec in expected.items():
        if not spec.get('gated') and not (root / path).is_file():
            errors.append('missing source asset: ' + path)
    # Reused FinFIRST is also verifiable by the official repository Git blob.
    card = json.loads((root / 'finfirst/source_info.json').read_text())
    native = (root / 'finfirst/test.jsonl').read_bytes()
    expected_blob = next(item['blobId'] for item in card['siblings'] if item['rfilename'] == 'test.jsonl')
    if hashlib.sha1(b'blob ' + str(len(native)).encode() + b'\0' + native).hexdigest() != expected_blob:
        errors.append('FinFIRST does not match official pinned HF blob')
    frontier = root / 'frontier_finance/frontier_finance_public.jsonl'
    if frontier.is_file():
        card = json.loads((root / 'frontier_finance/source_info.json').read_text())
        native = frontier.read_bytes()
        expected_blob = next(item['blobId'] for item in card['siblings'] if item['rfilename'] == frontier.name)
        if hashlib.sha1(b'blob ' + str(len(native)).encode() + b'\0' + native).hexdigest() != expected_blob:
            errors.append('FrontierFinance does not match official pinned HF blob')
    for benchmark, info in summary.items():
        if info['status'] == 'gated':
            checks[benchmark] = {'status': 'gated', 'reason': info['reason']}
            continue
        tasks = list(read_jsonl(root / info['tasks']))
        refs = list(read_jsonl(root / info['references']))
        lookup = {record['task_id']: record for record in refs}
        if len(lookup) != len(refs) or len(tasks) != info['task_count']:
            errors.append('task/reference count mismatch: ' + benchmark)
        cfg = load_config(root / 'eval_config.yaml', benchmark)
        cfg['reward']['weights'] = {key: int(key == 'answer_quality') for key in cfg['reward']['weights']}
        evaluator = get_evaluator(benchmark)
        valid = 0
        for task in tasks:
            identity = task['task_id']
            forbidden = {'answer', 'gold', 'rubric', 'Rubric', 'checklist', 'eval_input'} & set(task)
            if forbidden:
                errors.append(f'reference leakage into task index: {benchmark}/{identity}')
            for file in task.get('input_files', []):
                target = (root / file).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    errors.append(f'missing/invalid task material: {benchmark}/{identity}: {file}')
            if task.get('unresolved_document_paths'):
                errors.append(f'unresolved document reference: {benchmark}/{identity}')
            try:
                case = normalize_case({'benchmark': benchmark, 'objective': task['objective'],
                    'question_id': identity, **lookup[identity]['eval_input']})
                if benchmark == 'frontier_finance' and task.get('query_date') != case['row'].get('query_date'):
                    raise ValueError('task/reference query_date mismatch')
                # An empty candidate validates native input/parsers with zero
                # judge calls. This is a data check, not a model benchmark run.
                result = evaluator.score(case, '', {}, cfg)
                if result.judge['calls'] != 0:
                    raise ValueError('validation unexpectedly invoked a judge')
                if benchmark == 'dataclawbench':
                    from eval_finance.benchmarks.dataclawbench.inputs import process_gold
                    gold, _ = process_gold(case, cfg)
                    if not isinstance(gold.get('milestone'), dict) or not gold['milestone']:
                        raise ValueError('native process gold has no milestones')
                valid += 1
            except (KeyError, ValueError, TypeError, OSError) as exc:
                errors.append(f'invalid native evaluation input: {benchmark}/{identity}: {exc}')
        checks[benchmark] = {'status': 'checked', 'task_count': len(tasks), 'native_inputs_valid': valid}
        if material_report and benchmark in material_report['benchmarks']:
            checks[benchmark]['local_materials'] = material_report['benchmarks'][benchmark]
        print('VALIDATED', benchmark, valid, '/', len(tasks), flush=True)
    path = root / 'ddr_10k/raw/10k_financial_data.db'
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'companies', 'company_tickers', 'company_addresses', 'filings', 'financial_facts'}
        if not required <= tables:
            errors.append('DDR SQLite missing required tables')
        integrity = connection.execute('PRAGMA quick_check').fetchone()[0]
        if integrity != 'ok':
            errors.append('DDR SQLite quick_check failed: ' + integrity)
        columns = {row[1] for row in connection.execute('PRAGMA table_info(companies)')}
        if 'cik' in columns:
            companies = {str(row[0]).lstrip('0') for row in connection.execute('SELECT cik FROM companies')}
            entities = {str(value).lstrip('0') for value in json.loads((root / 'ddr_10k/entity_ids.json').read_text())}
            if not entities <= companies:
                errors.append('DDR task entities missing from SQLite companies')
    report = {'schema_version': 'eval_finance.data.validation.v1', 'status': 'passed' if not errors else 'failed',
              'file_count': len(inventory), 'total_bytes': sum(item['bytes'] for item in inventory),
              'benchmarks': checks, 'sqlite_quick_check': integrity, 'errors': errors,
              'verification': 'checksums, native inputs, task material paths' +
                  ('; FinanceAgent source dates, bundle isolation and price endpoints' if material_report else '') +
                  '; no live model or judge calls'}
    (root / 'manifest.json').write_text(json.dumps({'schema_version': 'eval_finance.data.manifest.v1',
        'benchmarks': summary, 'files': inventory}, ensure_ascii=False, indent=2) + '\n')
    (root / 'validation_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('Data validation:', report['status'], 'errors=', len(errors), flush=True)
    for error in errors[:20]:
        print(error, flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    args = parser.parse_args()
    raise SystemExit(int(validate(args.data_root.resolve())['status'] != 'passed'))
