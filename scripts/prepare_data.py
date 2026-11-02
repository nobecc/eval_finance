"""Extract native data and build separate task and evaluation-reference indexes."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import shutil
import stat
import zipfile
import yaml


def read_jsonl(path):
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def zip_name(info):
    name = info.filename
    # These official archives were produced on Windows with unflagged GBK
    # names. Python otherwise decodes them as CP437, breaking document paths.
    if not info.flag_bits & 0x800 and any(ord(char) > 127 for char in name):
        try:
            name = name.encode('cp437').decode('gbk')
        except (UnicodeError, LookupError):
            pass
    return name.replace('\\', '/')


def extract(root, spec):
    if spec.get('extract') is False:
        print('ARCHIVE ONLY', spec['path'], flush=True)
        return
    archive = root / spec['path']
    destination = (root / spec['destination']).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for info in source.infolist():
            suffixes = spec.get('extract_suffixes')
            if suffixes and not info.is_dir() and Path(info.filename).suffix.lower() not in suffixes:
                continue
            target = (destination / zip_name(info)).resolve()
            if not target.is_relative_to(destination) or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f'unsafe archive entry in {archive.name}')
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.is_file() or target.stat().st_size != info.file_size:
                    with source.open(info) as data, target.open('wb') as stream:
                        shutil.copyfileobj(data, stream, 1024 * 1024)
    print('EXTRACTED', spec['path'], flush=True)


def question(value):
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, str) or not value.strip():
        raise ValueError('native task question must be nonempty text')
    return value


def section(text, name):
    match = re.search(r'^##\s+' + re.escape(name) + r'\s*\n(.*?)(?=\n##\s|\Z)', text, re.M | re.S)
    return match.group(1).strip() if match else ''


def build(root):
    sources = json.loads((root / 'sources.json').read_text())
    for spec in sources['archives']:
        extract(root, spec)
    summary = {}
    def save(name, tasks, refs, **details):
        if len(tasks) != len(refs) or len({item['task_id'] for item in tasks}) != len(tasks):
            raise ValueError(f'duplicate or unmatched task identities: {name}')
        write_jsonl(root / name / 'tasks.jsonl', tasks)
        write_jsonl(root / name / 'references.jsonl', refs)
        summary[name] = {'status': 'ready', 'task_count': len(tasks),
                         'tasks': name + '/tasks.jsonl', 'references': name + '/references.jsonl', **details}
        print('INDEXED', name, len(tasks), flush=True)
    tasks, refs = [], []
    ids = json.loads((root / 'ddr_10k/entity_ids.json').read_text())
    qa = json.loads((root / 'ddr_10k/qa.json').read_text())['results']
    lookup = {str(row['entity_id']).lstrip('0'): row for row in qa}
    for entity in ids:
        entity = str(entity)
        tasks.append({'task_id': entity, 'objective': f'Analyze company with CIK {entity}',
                      'input_files': ['ddr_10k/raw/10k_financial_data.db']})
        refs.append({'task_id': entity, 'eval_input': {'entity_id': entity,
                     'checklist': lookup[entity.lstrip('0')]['qa_pairs']}})
    save('ddr_10k', tasks, refs, scope='all 10-K entity tasks')
    rows = json.loads((root / 'finsearchcomp/finsearchcomp_data.json').read_text())
    normalized = [{**row, 'prompt_id': [row['prompt_id']] if isinstance(row['prompt_id'], str) else row['prompt_id']} for row in rows]
    (root / 'finsearchcomp/native_rows.json').write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + '\n')
    for track in ['t2', 't3']:
        tasks, refs = [], []
        for row in normalized:
            if '(' + track.upper() + ')' not in str(row['prompt_id']):
                continue
            identity = str(row['prompt_id'][0])
            tasks.append({'task_id': identity, 'objective': question(row['prompt']), 'input_files': []})
            refs.append({'task_id': identity, 'eval_input': {'row': row}})
        save('finsearchcomp_' + track, tasks, refs, scope='full release, selected track')
    rows = list(read_jsonl(root / 'finfirst/test.jsonl'))
    save('finfirst', [{'task_id': str(row['id']), 'objective': question(row['query']), 'input_files': []} for row in rows],
         [{'task_id': str(row['id']), 'eval_input': {'row': row}} for row in rows], scope='public V1 test split')
    for benchmark in ['finance_agent_bench_v1_1', 'finance_agent_bench_v2']:
        with (root / benchmark / 'public.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        tasks, refs = [], []
        for index, row in enumerate(rows, 1):
            identity = f'public-{index:03d}'
            rubric = json.loads(row['Rubric'])
            native = {**row, 'Rubric': rubric}
            tasks.append({'task_id': identity, 'objective': question(row['Question']), 'input_files': []})
            refs.append({'task_id': identity, 'eval_input': {'row': native}})
        save(benchmark, tasks, refs, scope='official public subset; private validation/test not included')
        local = root / benchmark / 'tasks.local.jsonl'
        if local.is_file():
            local_tasks = list(read_jsonl(local))
            if {t['task_id'] for t in local_tasks} != {t['task_id'] for t in tasks}:
                raise ValueError('local material task identities differ: ' + benchmark)
            summary[benchmark]['local_tasks'] = benchmark + '/tasks.local.jsonl'
            summary[benchmark]['materials'] = benchmark + '/materials.jsonl'
            summary[benchmark]['local_material_task_count'] = sum(t['material_status'] == 'downloaded' for t in local_tasks)
    tasks, refs = [], []
    for path in sorted((root / 'dataclawbench/tasks').glob('*.md')):
        text = path.read_text()
        match = re.match(r'^---\s*\n(.*?)\n---', text, re.S)
        meta = yaml.safe_load(match.group(1))
        prompt = question(section(text, 'Prompt'))
        gold_path = root / 'dataclawbench/assets' / meta['gold_file']
        gold = json.loads(gold_path.read_text())
        files = [str((root / 'dataclawbench/assets' / item['source']).relative_to(root)) for item in meta.get('workspace_files', [])]
        tasks.append({'task_id': meta['id'], 'objective': prompt, 'input_files': files,
                      'input_mounts': [{'source': file, 'dest': item['dest']} for file, item in zip(files, meta.get('workspace_files', []))]})
        refs.append({'task_id': meta['id'], 'eval_input': {'objective': prompt,
            'expected_behavior': section(text, 'Expected Behavior'),
            'llm_judge_rubric': question(section(text, 'LLM Judge Rubric')), 'process_gold': gold},
            'native_task_file': str(path.relative_to(root)), 'native_gold_file': str(gold_path.relative_to(root))})
    save('dataclawbench', tasks, refs, scope='all native Markdown tasks and process gold')
    # Select only the task-category view, retaining the original scene view and
    # overall agent outputs on disk without counting them as additional tasks.
    native = root / 'finance_complex_qa/native/FinComplexQA-Pro'
    document_root = root / 'finance_complex_qa/native'
    documents = [path for path in document_root.rglob('*') if path.suffix.lower() in {'.pdf', '.tex'}]
    suffixes = {}
    for path in documents:
        relative = path.relative_to(root)
        for width in range(1, min(len(relative.parts), 6) + 1):
            suffixes.setdefault('/'.join(relative.parts[-width:]), []).append(str(relative))
    tasks, refs, seen, unresolved = [], [], set(), []
    for path in sorted(native.rglob('*.jsonl')):
        if 'task_categories' not in path.parts:
            continue
        lang = 'CN' if 'CN' in path.relative_to(native).parts else 'EN'
        for row in read_jsonl(path):
            identity = lang + '/' + str(row['finqa_id'])
            if identity in seen:
                raise ValueError(f'duplicate task-category ID: {identity}')
            seen.add(identity)
            raw_docs = row.get('Reference_documents', [])
            raw_docs = [raw_docs] if isinstance(raw_docs, str) else raw_docs
            files, missing = [], []
            for raw in raw_docs or []:
                value = str(raw).replace('\\', '/').strip('/')
                pieces = value.split('/')
                candidates = []
                for width in range(min(len(pieces), 6), 0, -1):
                    candidates = suffixes.get('/'.join(pieces[-width:]), [])
                    if candidates:
                        break
                if candidates:
                    # Duplicated official document bundles contain the same
                    # source names; prefer the public Reference_documents set.
                    candidates.sort(key=lambda name: ('/Reference_documents/' not in name, name))
                    chosen = candidates[0]
                    files.append(chosen)
                    sibling = str(Path(chosen).with_suffix('.pdf' if Path(chosen).suffix == '.tex' else '.tex'))
                    if (root / sibling).is_file():
                        files.append(sibling)
                else:
                    missing.append(raw)
            if missing:
                unresolved.append({'task_id': identity, 'native_paths': missing})
            tasks.append({'task_id': identity, 'objective': question(row['question']),
                          'input_files': sorted(set(files)), 'native_document_paths': raw_docs,
                          **({'unresolved_document_paths': missing} if missing else {})})
            refs.append({'task_id': identity, 'eval_input': {'row': row}, 'native_record_file': str(path.relative_to(root))})
    if not tasks:
        raise ValueError('FinanceComplexQA task-category view is missing')
    save('finance_complex_qa', tasks, refs, scope='FinComplexQA-Pro task-category view, CN and EN',
         unresolved_document_task_count=len(unresolved))
    write_jsonl(root / 'finance_complex_qa/unresolved_documents.jsonl', unresolved)
    frontier = root / 'frontier_finance/frontier_finance_public.jsonl'
    if frontier.is_file():
        rows = list(read_jsonl(frontier))
        save('frontier_finance', [{'task_id': str(row['query_id']), 'objective': question(row['query']),
                                  'query_date': row['query_date'], 'input_files': []} for row in rows],
             [{'task_id': str(row['query_id']), 'eval_input': {'row': row}} for row in rows], scope='authorized public dataset')
    else:
        summary['frontier_finance'] = {'status': 'gated', 'task_count': None,
            'source': 'https://huggingface.co/datasets/samaya-ai/FrontierFinance',
            'reason': 'Official HF authorization is required; user chose to defer this dataset.'}
    (root / 'index_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    args = parser.parse_args()
    build(args.data_root.resolve())
