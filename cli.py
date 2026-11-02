"""Shared scoring CLI; harnesses supply only their input adapter."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import EvaluationError
from .reward_calculator import FreeTextRewardCalculator
from .schema import REWARD_SCHEMA_VERSION
from .common import json_file as _json_file
from .registry import EVALUATORS


def main(argv: list[str] | None = None, *, load_run=None, export_adapter=None,
         calculator_factory=FreeTextRewardCalculator, schema_version=REWARD_SCHEMA_VERSION,
         log_name='eval_finance') -> int:
    parser = argparse.ArgumentParser(description='Score one finance benchmark task output')
    inputs = parser.add_mutually_exclusive_group()
    if load_run is not None:
        inputs.add_argument('--run-dir', help='completed harness run directory')
    else:
        parser.set_defaults(run_dir=None)
    if export_adapter is not None:
        parser.add_argument('--export-case', help='adapt and write a standard case JSON, without judging')
    inputs.add_argument('--case', help='unified evaluation case JSON file')
    parser.add_argument('--config', help='evaluation YAML (default: eval_finance/config.yaml)')
    parser.add_argument('--benchmark', help='override the benchmark profile')
    parser.add_argument('--entity-id', help='DDR CIK; inferred from the run objective when possible')
    parser.add_argument('--qa-file', help='DDR checklist qa.json; evaluation only')
    parser.add_argument('--rubric', help='native benchmark rubric JSON/text; DDR accepts checklist/rubric.v1')
    parser.add_argument('--row', help='one native benchmark dataset row JSON file; evaluation only')
    parser.add_argument('--list', action='store_true', help='list registered benchmark evaluators')
    parser.add_argument('--output', help='score JSON file (default: <run-dir>/reward.json; --case prints to stdout)')
    args = parser.parse_args(argv)
    exporting = export_adapter is not None and bool(args.export_case)
    if exporting and args.output:
        parser.error('--export-case cannot be combined with --output')
    if args.list:
        print('\n'.join(EVALUATORS))
        return 0
    if not args.run_dir and not args.case:
        parser.error('a task input (--case, or --run-dir for a harness) is required')
    case: dict = {}
    try:
        loaded = load_run(args.run_dir) if args.run_dir else _json_file(args.case)
        if not isinstance(loaded, dict):
            raise EvaluationError('case must be an object')
        case = loaded
        for key in ('benchmark', 'entity_id', 'qa_file'):
            if getattr(args, key) is not None:
                case[key] = getattr(args, key)
        if args.row:
            case['row_file'] = args.row
        if args.qa_file and case.get('benchmark') != 'ddr_10k':
            raise EvaluationError('--qa-file is DDR-only; supply the selected benchmark native row/rubric')
        if args.rubric:
            case['rubric_file'] = args.rubric
        if exporting:
            exported = export_adapter(case)
            if args.config:
                exported.setdefault('eval_params', {})['config_file'] = str(Path(args.config).resolve())
            target = Path(args.export_case)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(exported, ensure_ascii=False, indent=2) + '\n')
            print(f'[{log_name}] case={target}')
            return 0
        document = calculator_factory(args.config).evaluate_case(case)
        exit_code = 0
    except (EvaluationError, OSError, ValueError, TypeError) as exc:
        document = {'schema_version': schema_version, 'status': 'failed',
                    'benchmark': case.get('benchmark'), 'question_id': case.get('question_id'),
                    'entity_id': case.get('entity_id'), 'session_id': case.get('session_id'),
                    'raw_score': None, 'normalized_score_10': None, 'reason': str(exc),
                    'rollout_status': case.get('rollout_status', 'FINISHED'),
                    'evaluator': getattr(exc, 'evaluator', {}),
                    'components': getattr(exc, 'components', {}), 'metrics': getattr(exc, 'metrics', {}),
                    'judge': getattr(exc, 'judge_info', {}), 'artifacts': case.get('artifacts', {})}
        print(f'[{log_name}] {exc}', file=sys.stderr)
        exit_code = 1
    serialized = json.dumps(document, ensure_ascii=False, indent=2) + '\n'
    output = Path(args.output) if args.output else (Path(args.run_dir) / 'reward.json' if args.run_dir and not exporting else None)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized)
        print(f"[{log_name}] status={document['status']} raw_score={document['raw_score']} output={output}")
    else:
        print(serialized, end='')
    return exit_code
