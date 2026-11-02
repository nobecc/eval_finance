"""Shared case IO and judge transport accounting, without scoring policy."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import EvaluationError, resolve_judge
from .judge import JudgeClient


@dataclass
class BenchmarkScore:
    components: dict[str, Any]
    judge: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def component(score: float, weight: float, **detail: Any) -> dict[str, Any]:
    return {'status': 'scored', 'score': round(score, 6), 'weight': weight, 'detail': detail}


def not_applicable(weight: float, reason: str, **detail: Any) -> dict[str, Any]:
    return {'status': 'not_applicable', 'score': None, 'weight': weight,
            'detail': {'reason': reason, **detail}}


def answer_result(score: float, cfg: dict[str, Any], session: 'JudgeSession', metric: str,
                  *, warnings: list[str] | None = None, **metrics: Any) -> BenchmarkScore:
    return BenchmarkScore(
        {'answer_quality': component(score, cfg['reward']['weights']['answer_quality'], metric=metric)},
        session.info(), warnings or [], {metric: score, **metrics})


def json_file(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise EvaluationError(f'cannot read evaluation input {path}: {exc}') from exc


def field_value(case: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for source in (case, case.get('row') or {}):
        for key in keys:
            if source.get(key) is not None:
                return source[key]
    return default


def text_value(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError(f'{name} must be nonempty text')
    return value.strip()


def native_question(case: dict[str, Any]) -> str:
    value = field_value(case, 'query', 'Question', 'question', 'prompt', 'task', default=case.get('objective'))
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    return text_value(value, 'native benchmark question')


def reference_answer(case: dict[str, Any]) -> str:
    value = field_value(case, 'reference_answer', 'gold', 'Answer', 'answer', 'response_reference')
    if value is None:
        raise EvaluationError(f"{case['benchmark']} requires its reference answer")
    return text_value(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), 'reference answer')


def normalize_case(case: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise EvaluationError('evaluation case must be an object')
    if case.get('schema_version', 'eval_finance.case.v1') != 'eval_finance.case.v1':
        raise EvaluationError('unsupported case schema; expected eval_finance.case.v1')
    for key in ('row', 'artifacts', 'eval_params'):
        if not isinstance(case.get(key, {}), dict):
            raise EvaluationError(f'{key} must be an object')
    result = dict(case)
    for target, keys in [('objective', ('task', 'query', 'question', 'Question', 'prompt')),
                         ('entity_id', ('entity_id',)), ('question_id', ('id', 'query_id', 'finqa_id', 'prompt_id'))]:
        if not result.get(target):
            value = field_value(case, *keys)
            if value is not None:
                result[target] = value[0] if isinstance(value, list) and len(value) == 1 else value
    return result


def read_report(case: dict[str, Any]) -> str:
    if case.get('final_report') is not None:
        if not isinstance(case['final_report'], str):
            raise EvaluationError('final_report must be free text')
        return case['final_report']
    artifacts = case.get('artifacts') or {}
    if artifacts.get('final_report'):
        return Path(artifacts['final_report']).read_text()
    return ''


def load_native_inputs(case: dict[str, Any]) -> dict[str, Any]:
    """CLI reference files are read only when a native scoring item is active."""
    if case.get('row_file'):
        row = json_file(case['row_file'])
        if not isinstance(row, dict):
            raise EvaluationError('--row must contain one native dataset record object')
        case['row'] = row
    if case.get('rubric_file'):
        text = Path(case['rubric_file']).read_text()
        try:
            rubric = json.loads(text)
        except ValueError:
            rubric = text
        if case['benchmark'] == 'ddr_10k':
            if isinstance(rubric, list):
                case.pop('reward_rubric', None)
                if isinstance(case.get('row'), dict):
                    case['row'] = {key: value for key, value in case['row'].items() if key != 'reward_rubric'}
            case['checklist' if isinstance(rubric, list) else 'reward_rubric'] = rubric
        else:
            case['rubric'] = rubric
    return normalize_case(case)


class JudgeSession:
    """Account for calls; each benchmark owns prompts, parsing and aggregation."""
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.calls = self.failed = self.attempts = 0
        self.started = time.monotonic()
        self.models: list[str] = []
        self.attempt_history: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self._settings = None
        config['_judge_session'] = self

    @property
    def settings(self) -> dict[str, Any]:
        if self._settings is None:
            self._settings = resolve_judge(self.config['judge'], self.config['benchmark'])
        return self._settings

    def ask(self, system: str, user: str, validator, judge_config: dict[str, Any] | None = None) -> dict[str, Any]:
        config = judge_config or self.settings
        value = JudgeClient(config).ask(system, user, validator)
        with self.lock:
            self.calls += 1
            self.failed += value.get('context_quality') == 'API_FAILED'
            self.attempts += value.get('_attempts', 0)
            self.attempt_history.extend({'model': config['model'], **entry}
                                       for entry in value.get('_attempt_history', []))
            if config['model'] not in self.models:
                self.models.append(config['model'])
        return value

    def info(self, **extra: Any) -> dict[str, Any]:
        return {'model': self.models[0] if self.models else '', 'models': list(self.models),
                'calls': self.calls, 'failed': self.failed,
                'attempts': self.attempts, 'duration_s': round(time.monotonic() - self.started, 3),
                'attempt_history': list(self.attempt_history), **extra}

    def require(self, value: dict[str, Any]) -> dict[str, Any]:
        if value.get('context_quality') == 'API_FAILED':
            raise EvaluationError(f'judge failed: {value.get("error")}', judge_info=self.info())
        return value
