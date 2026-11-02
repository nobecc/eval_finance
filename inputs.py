"""Lazy access to harness-neutral output; native references stay in the case.

Harness adapters may subclass CaseInputs to translate their output on demand.
Exported cases use only JSON values and can be scored without an adapter.
"""
from __future__ import annotations

from typing import Any
from .common import json_file, read_report
from .config import EvaluationError

CASE_SCHEMA_VERSION = 'eval_finance.case.v1'


class CaseInputs:
    def __init__(self, case: dict[str, Any]):
        self.case = case
        self._trajectory: dict[str, Any] | None = None

    def read_report(self) -> str:
        return read_report(self.case)

    def trajectory(self, *, evidence: bool = False, protocol: bool = False,
                   steps: bool = False) -> dict[str, Any]:
        if self._trajectory is None:
            value = self.case.get('trajectory')
            if value is None:
                path = self.case.get('artifacts', {}).get('trajectory')
                value = json_file(path) if path else {}
            if not isinstance(value, dict):
                raise EvaluationError('trajectory must be an object')
            self._trajectory = value
        return self._trajectory
