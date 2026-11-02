"""Unified reward envelope and weighted aggregation; no benchmark policy."""
from __future__ import annotations

import math
from typing import Any
from .config import EvaluationError
from .components import COMPONENT_NAMES

REWARD_SCHEMA_VERSION = 'eval_finance.reward.v1'
COMPONENTS = COMPONENT_NAMES


def reward_document(case: dict[str, Any], cfg: dict[str, Any], components: dict[str, Any],
                    judge: dict[str, Any], warnings: list[str], metrics: dict[str, Any],
                    evaluator: dict[str, Any], reward_range: tuple[float, float]) -> dict[str, Any]:
    unknown = set(components) - set(COMPONENTS)
    if unknown:
        raise EvaluationError(f'unknown reward components: {sorted(unknown)}')
    total = 0.0
    for name, value in components.items():
        if not isinstance(value, dict) or type(value.get('weight')) not in (int, float):
            raise EvaluationError(f'invalid reward component: {name}')
        if not math.isfinite(value['weight']) or value['weight'] < 0:
            raise EvaluationError(f'nonfinite or negative component weight: {name}')
        if value['weight'] != cfg['reward']['weights'][name]:
            raise EvaluationError(f'evaluator changed configured weight: {name}')
        status = value.get('status')
        if status == 'disabled':
            if value['weight'] != 0 or value.get('score') is not None:
                raise EvaluationError(f'invalid disabled component: {name}')
            value['contribution'] = 0.0
        elif status == 'not_applicable':
            if value['weight'] <= 0 or value.get('score') is not None:
                raise EvaluationError(f'invalid inapplicable component: {name}')
            value['contribution'] = 0.0
        elif status == 'scored':
            score = value.get('score')
            if value['weight'] <= 0 or type(score) not in (int, float) or not math.isfinite(score):
                raise EvaluationError(f'invalid scored component: {name}')
            lo, hi = value['score_range']
            if score < lo - 1e-6 or score > hi + 1e-6:
                raise EvaluationError(f'component score outside declared range: {name}')
            total += score * value['weight']
            value['contribution'] = round(score * value['weight'], 6)
        else:
            raise EvaluationError(f'invalid component status: {name}: {status}')
    lower, upper = reward_range
    if (not math.isfinite(total) or not math.isfinite(lower) or not math.isfinite(upper)
            or lower >= upper or not math.isfinite(upper - lower)):
        raise EvaluationError('invalid weighted reward range')
    if total < lower - 1e-6 or total > upper + 1e-6:
        raise EvaluationError('weighted reward outside declared range')
    benchmark = case.get('benchmark') or 'generic'
    return {'schema_version': REWARD_SCHEMA_VERSION, 'status': 'succeeded', 'benchmark': benchmark,
        'question_id': case.get('question_id'), 'entity_id': str(case.get('entity_id') or ''),
        'session_id': case.get('session_id'), 'raw_score': round(total, 6),
        'normalized_score_10': round(max(0.0, min(10.0, (total - lower) / (upper - lower) * 10.0)), 4),
        'reason': f'benchmark={benchmark}; configured weighted reward components',
        'rollout_status': case.get('rollout_status', 'FINISHED'), 'evaluator': evaluator,
        'components': components, 'metrics': metrics,
        'judge': judge, 'warnings': warnings,
        'artifacts': {**(case.get('artifacts') or {}), 'evaluator': evaluator['id'],
                      'config_file': cfg['path'], 'benchmark_config_file': cfg.get('benchmark_config_file'),
                      'reward_range': [lower, upper]}}
