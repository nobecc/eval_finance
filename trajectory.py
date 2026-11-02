"""Protocol checks and judge context from normalized tool/assistant records."""
from __future__ import annotations

import json
from collections import Counter
from typing import Any
from .config import EvaluationError


def records(trajectory: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = trajectory.get(key, [])
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise EvaluationError(f'trajectory.{key} must be a list of objects')
    return values


def _protocol(trajectory: dict[str, Any], allowed_tools: list[str]) -> dict[str, Any]:
    declarations = records(trajectory, 'tool_calls')
    starts, ends = {}, {}
    executions = records(trajectory, 'tool_executions')
    for index, execution in enumerate(executions):
        phase = execution.get('phase')
        if phase not in {'start', 'end'}:
            raise EvaluationError('tool execution phase must be start or end')
        call_id = str(execution.get('call_id') or '')
        target = starts if phase == 'start' else ends
        target.setdefault(call_id, []).append((index, execution))
    ids = Counter(str(call.get('id') or '') for call in declarations)
    valid, issues = 0, []
    for call in declarations:
        call_id, name = str(call.get('id') or ''), call.get('name')
        problems = []
        if not call_id or ids[call_id] != 1:
            problems.append('missing_or_duplicate_call_id')
        if not isinstance(name, str) or not name.strip() or (allowed_tools and name not in allowed_tools):
            problems.append('unknown_or_missing_tool_name')
        if not isinstance(call.get('arguments'), dict):
            problems.append('arguments_not_object')
        before, after = starts.get(call_id, []), ends.get(call_id, [])
        if len(before) != 1 or len(after) != 1:
            problems.append('missing_or_duplicate_execution_pair')
        else:
            if before[0][1].get('name') != name or after[0][1].get('name') != name:
                problems.append('execution_name_mismatch')
            if before[0][1].get('arguments') != call.get('arguments'):
                problems.append('execution_arguments_mismatch')
            if 'result' not in after[0][1]:
                problems.append('missing_tool_result')
            if before[0][0] >= after[0][0]:
                problems.append('execution_end_before_start')
        if problems:
            issues.append({'tool_call_id': call_id, 'problems': problems})
        else:
            valid += 1
    orphans = (set(starts) | set(ends)) - set(ids)
    for call_id in sorted(orphans):
        issues.append({'tool_call_id': call_id, 'problems': ['execution_without_assistant_call']})
    return {'attempted_calls': len(declarations) + len(orphans), 'valid_calls': valid, 'issues': issues,
            'execution_errors': sum(bool(item.get('is_error')) for item in executions if item['phase'] == 'end')}


def load_trajectory(trajectory: dict[str, Any], allowed_tools: list[str], max_message_chars: int,
                    *, evidence: bool = True, protocol: bool = True) -> dict[str, Any]:
    checks = _protocol(trajectory, allowed_tools) if protocol else {}
    source = 'none'
    message_tool_call_ids: list[list[str]] = []
    if evidence and 'message_wise_insights' in trajectory:
        turns = records(trajectory, 'message_wise_insights')
        source = 'message_wise_insights'
        messages = []
        for index, turn in enumerate(turns):
            text = turn.get('text')
            call_ids = turn.get('tool_call_ids', [])
            if not isinstance(text, str) or not text.strip():
                raise EvaluationError(f'trajectory.message_wise_insights[{index}].text must be nonempty text')
            if (not isinstance(call_ids, list) or not call_ids
                    or any(not isinstance(call_id, str) or not call_id for call_id in call_ids)):
                raise EvaluationError(
                    f'trajectory.message_wise_insights[{index}].tool_call_ids must be nonempty strings')
            messages.append(text.strip())
            message_tool_call_ids.append(list(dict.fromkeys(call_ids)))
    else:
        turns = records(trajectory, 'interactions') if evidence else []
        source = 'raw_interactions' if evidence else 'none'
        messages = []
        for turn in turns:
            messages.append(
                f"Tool: {turn.get('tool_name', '')}\nTool call id: {turn.get('tool_call_id', '')}\n"
                f"Assistant reason/action: {turn.get('assistant_message', '')}\n"
                f"Arguments: {json.dumps(turn.get('tool_arguments', {}), ensure_ascii=False)}\n"
                f"Tool execution error: {turn.get('is_error', False)}\n"
                f"Observed result: {json.dumps(turn.get('tool_result'), ensure_ascii=False)}")
            message_tool_call_ids.append([turn.get('tool_call_id', '')])
    clipped = []
    for index, text in enumerate(messages):
        if max_message_chars and len(text) > max_message_chars:
            marker = ('[MESSAGE-WISE INSIGHT CLIPPED BY EVALUATOR]' if source == 'message_wise_insights'
                      else '[TOOL INTERACTION CLIPPED BY EVALUATOR]')
            messages[index] = text[:max_message_chars] + '\n' + marker
            clipped.append(index)
    return {'messages': messages,
            'numbered_context': '\n\n'.join(f'[Message {index}]:\n{text}' for index, text in enumerate(messages)),
            'tool_call_ids': [ids[0] if ids else '' for ids in message_tool_call_ids],
            'message_tool_call_ids': message_tool_call_ids,
            'context_source': source,
            'truncated_message_indices': clipped,
            'format_score': checks.get('valid_calls', 0) / checks['attempted_calls'] if checks.get('attempted_calls') else 0.0,
            'protocol': checks}


def load_steps(trajectory: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """One normalized assistant turn per step, including the final answer."""
    steps, clipped = [], []
    for index, record in enumerate(records(trajectory, 'assistant_steps'), 1):
        calls = records(record, 'tool_calls')
        results = record.get('tool_results', [])
        if (not isinstance(record.get('text', ''), str) or not isinstance(results, list)
                or any(not isinstance(result, str) for result in results)):
            raise EvaluationError('assistant steps require text and a list of tool result strings')
        context = '\n'.join([record.get('text', ''),
            *('Tool call: ' + json.dumps(call, ensure_ascii=False) for call in calls), *results])
        if max_chars and len(context) > max_chars:
            context = context[:max_chars] + '\n[ASSISTANT STEP CLIPPED BY EVALUATOR]'
            clipped.append(index)
        steps.append({'index': index, 'context': context,
                      'tool_calls': [{'id': call.get('id'), 'name': call.get('name')} for call in calls]})
    return {'steps': steps, 'truncated_step_indices': clipped}
