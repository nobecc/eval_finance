"""FinanceAgent native rubric inputs; v1.1 and v2 own different aggregation."""
import json
from ..common import field_value
from ..config import EvaluationError


def criteria(case, operator):
    items = field_value(case, 'rubric', 'Rubric', 'rubrics')
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError as exc:
            raise EvaluationError('FinanceAgent Rubric must be a JSON criterion list') from exc
    if not isinstance(items, list) or not items:
        raise EvaluationError(f'{case["benchmark"]} requires its native Rubric list')
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('criteria'), str) or not item['criteria'].strip():
            raise EvaluationError('FinanceAgent rubric entries require criteria text')
        if item.get('operator') not in operator:
            raise EvaluationError(f'unsupported {case["benchmark"]} operator: {item.get("operator")}')
    return items


def judge_rubric_prompt(items, key):
    """Serialize native checks with explicit IDs and a matching response contract.

    IDs belong to the judge transport only. Native rubric records, operators,
    modifiers and benchmark aggregation remain unchanged.
    """
    ids = [str(index) for index in range(len(items))]
    value_schema = {'type': 'boolean'} if key == 'label' else {
        'type': 'number', 'minimum': 0, 'maximum': 1}
    judgment = {'type': 'object', 'properties': {
        key: value_schema, 'reason': {'type': 'string'}}, 'required': [key],
        'additionalProperties': False}
    schema = {'type': 'object', 'properties': {index: judgment for index in ids},
              'required': ids, 'additionalProperties': False}
    return ('Rubric by zero-based criterion ID:\n'
            + json.dumps(dict(zip(ids, items)), ensure_ascii=False)
            + '\n\nReturn exactly one judgment for every supplied ID. Copy the IDs '
              'exactly, starting at "0"; do not renumber, omit or add IDs. '
              'Return the judgment object directly, without a wrapper or array. '
              'Provide a brief reason for each judgment when possible.\n'
              'Required response JSON Schema:\n'
            + json.dumps(schema, ensure_ascii=False))
