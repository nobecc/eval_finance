"""Primitive judge-response validation; metric definitions stay in benchmarks."""
import math
from typing import Any
from .config import EvaluationError


def unit_score(value: Any, name: str = 'score') -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise EvaluationError(f'{name} must be a finite number in [0,1]')
    return float(value)


def indexed(value: dict[str, Any], count: int, key: str = 'label') -> dict[str, Any]:
    expected = [str(i) for i in range(count)]
    if set(value) != set(expected):
        # Only bounded key diagnostics enter logs/retry instructions. Preserve
        # strict completeness; never shift IDs, drop extras or fill missing scores.
        actual = [str(item)[:80] for item in list(value)[:20]]
        raise EvaluationError('judge must return exactly one result for each supplied criterion index; '
                              f'expected_keys={expected}; received_keys={actual}'
                              + (' (first 20)' if len(value) > 20 else ''))
    for item in value.values():
        if not isinstance(item, dict):
            raise EvaluationError('criterion judgment must be an object')
        if key == 'label':
            if type(item.get(key)) is not bool:
                raise EvaluationError('criterion label must be boolean')
        else:
            unit_score(item.get(key), key)
    return value
