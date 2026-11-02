"""DDR checklist loading and label aggregation; not used by other benchmarks."""
from __future__ import annotations
import hashlib,json,re
from pathlib import Path
from typing import Any
from ...config import EvaluationError, resource_path
from ...common import json_file as _json_file
from .judge import QUALITY_TO_REWARD

CHECKLIST_SCHEMA_VERSION = 'eval_finance.ddr_checklist.v1'

def _compact(value: Any) -> str:
    return re.sub(r'\s+', ' ', value).strip() if isinstance(value, str) else ''

def _entity(value: Any) -> str:
    text = str(value or '').strip()
    return text.lstrip('0') or '0' if text.isdigit() else text

def _validate_pairs(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list) or not items:
        raise EvaluationError('checklist must contain question/answer pairs')
    result = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not _compact(item.get('question')) or not _compact(item.get('answer')):
            raise EvaluationError(f'checklist item {i} must have nonempty question and answer strings')
        result.append({'question': _compact(item['question']), 'answer': _compact(item['answer'])})
    return result

def checklist(case: dict[str, Any], config: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
    row = case.get('row') or {}
    rubric = case.get('reward_rubric', row.get('reward_rubric'))
    if rubric is not None:
        if not isinstance(rubric, dict) or rubric.get('schema_version') not in {CHECKLIST_SCHEMA_VERSION, 'pi_finance.ddr_checklist.v1'} or rubric.get('kind') != 'checklist':
            raise EvaluationError(f'reward_rubric must be a {CHECKLIST_SCHEMA_VERSION} checklist')
        stamped = rubric.get('entity_id')
        if stamped is not None and _entity(stamped) != _entity(case.get('entity_id')):
            raise EvaluationError('reward_rubric entity_id does not match the case')
        pairs = _validate_pairs(rubric.get('items'))
        digest = hashlib.sha256(json.dumps(pairs, ensure_ascii=False, sort_keys=True,
                                          separators=(',', ':')).encode()).hexdigest()[:16]
        if rubric.get('digest') and rubric['digest'] != digest:
            raise EvaluationError('reward_rubric digest mismatch')
        return pairs, 'row_reward_rubric'
    if 'checklist' in case:
        return _validate_pairs(case['checklist']), 'case_checklist'
    if case.get('benchmark') == 'ddr_10k':
        entity_id = _entity(case.get('entity_id'))
        if not entity_id:
            raise EvaluationError('ddr_10k requires entity_id (CIK)')
        qa_file = resource_path(config, 'qa_file', case.get('qa_file'))
        if not qa_file:
            raise EvaluationError('ddr_10k requires a checklist rubric or qa_file')
        data = _json_file(qa_file)
        records = data.get('results', []) if isinstance(data, dict) else data
        if not isinstance(records, list):
            raise EvaluationError('DDR qa_file must contain a results list')
        matches = [item for item in records if isinstance(item, dict) and _entity(item.get('entity_id')) == entity_id]
        if len(matches) != 1:
            raise EvaluationError(f'DDR qa_file must have exactly one checklist for CIK {entity_id}; found {len(matches)}')
        return _validate_pairs(matches[0].get('qa_pairs')), str(Path(qa_file).resolve())
    raise EvaluationError("ddr_10k requires its checklist")


def _aggregate(judgments: list[dict[str, Any]]) -> tuple[float, dict[str, Any]]:
    counts = {key: sum(item.get('context_quality') == key for item in judgments)
              for key in [*QUALITY_TO_REWARD, 'API_FAILED']}
    # Failed judgments remain in the denominator (zero contribution).
    # No heuristic replaces a missing financial judgment.
    score = sum(QUALITY_TO_REWARD.get(item.get('context_quality'), 0.0) for item in judgments) / len(judgments) if judgments else 0.0
    return score, {'items': len(judgments), 'correct': counts['CORRECT_INFO'],
                   'incorrect': counts['INCORRECT_INFO'], 'insufficient': counts['INSUFFICIENT_INFO'],
                   'failed': counts['API_FAILED'],
                   'accuracy': counts['CORRECT_INFO'] / len(judgments) if judgments else 0.0,
                   'judgments': judgments}
