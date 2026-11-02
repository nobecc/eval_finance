"""Dispatch benchmark-owned scoring and serialize the unified reward schema."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import math
from .common import normalize_case, BenchmarkScore, load_native_inputs
from .config import EvaluationError, load_config
from .registry import get_evaluator
from .schema import REWARD_SCHEMA_VERSION, reward_document
from .trajectory import load_trajectory
from .inputs import CaseInputs


class FreeTextRewardCalculator:
    def __init__(self, config_path: str | Path | None = None):
        self.config_path = config_path

    def evaluate_case(self, case: dict[str, Any], *, inputs: CaseInputs | None = None) -> dict[str, Any]:
        case = normalize_case(case)
        inputs = inputs or CaseInputs(case)
        benchmark = case.get('benchmark') or 'generic'
        if not isinstance(benchmark, str):
            raise EvaluationError('benchmark must be a profile identifier')
        evaluator = get_evaluator(benchmark)
        case['benchmark'] = benchmark
        params = case.get('eval_params', {})
        config_path = self.config_path or params.get('config_file')
        cfg = load_config(config_path, benchmark)
        cfg['_inputs'] = inputs
        weights = cfg['reward']['weights']
        ranges = {**evaluator.component_ranges, 'tool_protocol': (0.0, 1.0)}
        active = {name for name, weight in weights.items() if weight > 0}
        unsupported = active - set(ranges)
        if unsupported:
            raise EvaluationError(f'{benchmark} does not support components: {sorted(unsupported)}')
        evaluator_info = {'id': f'eval_finance.{benchmark}', 'version': evaluator.version}
        reward_range = evaluator.reward_bounds(weights)
        if (any(not math.isfinite(x) for x in reward_range) or reward_range[0] >= reward_range[1]
                or not math.isfinite(reward_range[1] - reward_range[0])):
            raise EvaluationError('configured weights produce an invalid reward range')
        status = str(case.get('rollout_status', 'FINISHED')).upper()
        if status not in {'FINISHED', 'TRUNCATED', 'ABORTED'}:
            raise EvaluationError(f'unknown rollout_status: {status}')
        case['rollout_status'] = status
        components = {name: {'status': 'disabled' if weights[name] == 0 else 'pending',
            'score': None, 'weight': weights[name], 'score_range': list(bounds),
            'detail': {'reason': 'weight is zero'} if weights[name] == 0 else {}}
            for name, bounds in ranges.items()}
        score = BenchmarkScore({}, {'model': '', 'models': [], 'calls': 0, 'failed': 0, 'attempts': 0, 'duration_s': 0})
        warnings = []
        try:
            if active & set(evaluator.component_ranges):
                case = load_native_inputs(case)
            report = inputs.read_report() if 'answer_quality' in active else ''
            limit = cfg['reward']['max_report_chars']
            if limit and len(report) > limit:
                warnings.append(f'report clipped from {len(report)} to {limit} characters by explicit evaluation config')
                report = report[:limit]
            trajectory = {}
            if active & {'trajectory_evidence', 'tool_protocol'}:
                tools = case.get('tools', [])
                if not isinstance(tools, list) or any(not isinstance(name, str) for name in tools):
                    raise EvaluationError('tools must be a list of tool names')
                trajectory = load_trajectory(inputs.trajectory(evidence='trajectory_evidence' in active,
                    protocol='tool_protocol' in active), tools,
                    cfg['reward']['max_message_chars'], evidence='trajectory_evidence' in active,
                    protocol='tool_protocol' in active)
                if trajectory['truncated_message_indices']:
                    label = ('message-wise insights' if trajectory['context_source'] == 'message_wise_insights'
                             else 'tool interactions')
                    warnings.append(label + ' clipped at indices ' + str(trajectory['truncated_message_indices']))
                if 'tool_protocol' in active:
                    components['tool_protocol'].update({'status': 'scored', 'score': round(trajectory['format_score'], 6),
                        'detail': {'metric': 'valid_tool_call_ratio'}})
                    score.metrics['tool_protocol'] = trajectory['protocol']
            if active & set(evaluator.component_ranges):
                result = evaluator.score(case, report, trajectory, cfg)
                expected = active & set(evaluator.component_ranges)
                extra = set(result.components) - expected
                missing = expected - set(result.components)
                if extra or missing:
                    raise EvaluationError(f'evaluator component contract mismatch: extra={sorted(extra)}, missing={sorted(missing)}')
                for name, value in result.components.items():
                    components[name].update(value)
                score.judge = result.judge
                score.metrics.update(result.metrics)
                warnings.extend(result.warnings)
            return reward_document(case, cfg, components, score.judge, warnings,
                                   score.metrics, evaluator_info, reward_range)
        except (EvaluationError, OSError, ValueError, TypeError) as exc:
            error = exc if isinstance(exc, EvaluationError) else EvaluationError(str(exc))
            for name, value in error.components.items():
                if name in components:
                    components[name].update(value)
            score.metrics.update(error.metrics)
            for value in components.values():
                if value['status'] == 'pending':
                    value.update(status='failed', detail={'reason': str(error)})
                value['contribution'] = (round(value['score'] * value['weight'], 6)
                    if value['status'] == 'scored' else (None if value['status'] == 'failed' else 0.0))
            error.components = components
            error.metrics = score.metrics
            error.evaluator = evaluator_info
            if not error.judge_info:
                error.judge_info = cfg['_judge_session'].info() if '_judge_session' in cfg else score.judge
            raise error

    _document = staticmethod(reward_document)


EVALUATOR = FreeTextRewardCalculator()
