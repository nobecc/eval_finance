"""Evaluation configuration, separate from agent/runtime configuration."""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import yaml
from .components import COMPONENT_NAMES


class EvaluationError(ValueError):
    """An invalid input or unavailable judge prevents reliable scoring."""

    def __init__(self, message: str, *, judge_info: dict[str, Any] | None = None):
        super().__init__(message)
        self.judge_info = judge_info or {}
        self.components = {}
        self.metrics = {}
        self.evaluator = {}


DEFAULT_CONFIG = Path(__file__).with_name('config.yaml')


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EvaluationError(f'{name} must be an object')
    if any(not isinstance(key, str) for key in value):
        raise EvaluationError(f'{name} keys must be strings')
    return value


def _resolve(value: Any) -> Any:
    if isinstance(value, str) and value.startswith('$'):
        name = value[1:].strip('{}')
        if not name or name not in os.environ:
            raise EvaluationError(f'evaluation environment reference {name!r} is not set')
        return os.environ[name]
    return value


def _number(value: Any, name: str, minimum: float, *, integer: bool = False) -> float | int:
    if isinstance(value, bool):
        raise EvaluationError(f'{name} must be numeric')
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise EvaluationError(f'{name} must be numeric') from None
    if not math.isfinite(number) or number < minimum or (integer and number != int(number)):
        raise EvaluationError(f'{name} must be finite and >= {minimum}' + (' (integer)' if integer else ''))
    return int(number) if integer else number


def judge_environment(values: dict[str, Any], benchmark: str) -> dict[str, Any]:
    """Apply environment precedence without resolving unused base references."""
    result = dict(values)
    for key in {*result, 'max_tokens_limit', 'total_timeout_s'}:
        override = os.getenv('EVAL_FINANCE_JUDGE_' + key.upper())
        if override is None:
            override = os.getenv('PI_FINANCE_EVAL_JUDGE_' + key.upper())
        if override is None and benchmark == 'ddr_10k':
            override = os.getenv('DDR_REWARD_JUDGE_' + {'timeout_s': 'TIMEOUT_SECONDS'}.get(key, key.upper()))
        if override is not None:
            result[key] = override
    return result


def resolve_judge(values: dict[str, Any], benchmark: str, base: dict[str, Any] | None = None, *, environment: bool = True) -> dict[str, Any]:
    judge = {'model': '', 'base_url': '', 'api_key': '', 'timeout_s': 600, 'max_tokens': 8192,
             'max_retries': 2, 'concurrency': 4, **(base or {}), **_mapping(values, 'judge')}
    unknown = set(judge) - {'model', 'base_url', 'api_key', 'timeout_s', 'total_timeout_s',
                            'max_tokens', 'max_tokens_limit', 'max_retries', 'concurrency'}
    if unknown:
        raise EvaluationError(f'unknown judge keys: {sorted(unknown)}')
    if environment:
        judge = judge_environment(judge, benchmark)
    judge = {key: _resolve(value) for key, value in judge.items()}
    for key in ('model', 'base_url', 'api_key'):
        if not isinstance(judge[key], str):
            raise EvaluationError(f'judge.{key} must be text')
    for key, minimum in [('timeout_s', 0.001), ('max_tokens', 1), ('max_retries', 0), ('concurrency', 1)]:
        judge[key] = _number(judge[key], f'judge.{key}', minimum, integer=key != 'timeout_s')
    judge['max_tokens_limit'] = _number(judge.get('max_tokens_limit', judge['max_tokens']),
                                        'judge.max_tokens_limit', judge['max_tokens'], integer=True)
    judge['total_timeout_s'] = _number(judge.get('total_timeout_s',
        judge['timeout_s'] * (judge['max_retries'] + 1) + 2 * judge['max_retries']),
        'judge.total_timeout_s', 0.001)
    if judge['concurrency'] > 64 or judge['max_retries'] > 10:
        raise EvaluationError('judge concurrency must be <= 64 and max_retries <= 10')
    return judge


def _yaml(path: Path) -> dict[str, Any]:
    try:
        return _mapping(yaml.safe_load(path.read_text()), str(path))
    except (OSError, yaml.YAMLError) as exc:
        raise EvaluationError(f'cannot load evaluation config {path}: {exc}') from exc


def load_config(path: str | Path | None, benchmark: str) -> dict[str, Any]:
    config_path = Path(path or os.getenv('EVAL_FINANCE_CONFIG') or os.getenv('PI_FINANCE_EVAL_CONFIG') or DEFAULT_CONFIG).resolve()
    raw = _yaml(config_path)
    shared = raw if config_path == DEFAULT_CONFIG.resolve() else _yaml(DEFAULT_CONFIG)
    own_path = DEFAULT_CONFIG.parent / 'benchmarks' / benchmark / 'config.yaml'
    own = _yaml(own_path) if own_path.is_file() else {}
    from .registry import EVALUATORS
    names = set(EVALUATORS)
    unknown = set(raw) - {'judge', 'reward', 'fallback_judge', 'generic', *names}
    if unknown:
        raise EvaluationError(f'unknown evaluation config keys: {sorted(unknown)}')
    override = _mapping(raw.get(benchmark), benchmark)
    # User-supplied global weights override benchmark defaults. The checked-in
    # shared defaults precede those defaults, so DDR still defaults to 0.4.
    layers = [shared, own]
    if config_path != DEFAULT_CONFIG.resolve():
        layers.append(raw)
    layers.append(override)
    judge = {}
    reward = {'weights': {name: 0.0 for name in COMPONENT_NAMES},
              'max_report_chars': 0, 'max_message_chars': 16384}
    for layer in layers:
        judge.update(_mapping(layer.get('judge'), 'judge'))
        values = _mapping(layer.get('reward'), 'reward')
        unknown_reward = set(values) - set(reward)
        if unknown_reward:
            raise EvaluationError(f'unknown reward keys: {sorted(unknown_reward)}; use reward.weights with v2 component names')
        weights = _mapping(values.get('weights'), 'reward.weights')
        if set(weights) - set(COMPONENT_NAMES):
            raise EvaluationError(f'unknown reward components: {sorted(set(weights) - set(COMPONENT_NAMES))}')
        reward['weights'].update(weights)
        reward.update({key: value for key, value in values.items() if key != 'weights'})
    for name, value in reward['weights'].items():
        reward['weights'][name] = _number(_resolve(value), f'reward.weights.{name}', 0)
    if not any(reward['weights'].values()):
        raise EvaluationError('at least one reward component must have positive weight')
    for key in ('max_report_chars', 'max_message_chars'):
        reward[key] = _number(_resolve(reward[key]), f'reward.{key}', 0, integer=True)
    profile = {**own, **override}
    options = {key: value for key, value in profile.items() if key not in {'judge', 'reward'}}
    if 'reward_mode' in options:
        raise EvaluationError('reward_mode was removed; select components using reward.weights')
    # Resolve endpoint/environment references and resource paths only when used.
    option_bases = {key: str(config_path.parent if key in override else own_path.parent) for key in options}
    fallback = raw.get('fallback_judge', False)
    if not isinstance(fallback, bool):
        raise EvaluationError('fallback_judge must be a boolean')
    return {'path': str(config_path), 'benchmark_config_file': str(own_path),
            'benchmark': benchmark, 'judge': judge, 'reward': reward, 'options': options,
            'option_bases': option_bases, 'fallback_judge': fallback}


def resource_path(cfg: dict[str, Any], key: str, supplied: Any = None) -> str | None:
    """Lazily resolve a resource relative to the YAML that supplied it."""
    value = supplied if supplied is not None else cfg['options'].get(key)
    if supplied is None and key == 'qa_file':
        value = os.getenv('EVAL_FINANCE_QA_FILE') or os.getenv('PI_FINANCE_EVAL_QA_FILE') or value
    value = _resolve(value)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError(f'{key} must be a nonempty path')
    base = Path.cwd() if supplied is not None else Path(cfg['option_bases'].get(key, Path(cfg['path']).parent))
    return str((base / Path(value).expanduser()).resolve())
