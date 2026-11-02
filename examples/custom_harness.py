"""Example: translate a different harness output once, then reuse all scorers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from eval_finance import CASE_SCHEMA_VERSION


def adapt_output(output: dict, benchmark: str) -> dict:
    """This sample harness supplies completed tool calls grouped by turn."""
    declarations, executions, interactions, steps = [], [], [], []
    for turn in output.get('trace', []):
        calls, results = [], []
        for call in turn.get('calls', []):
            declaration = {'id': call['call_id'], 'name': call['tool'], 'arguments': call['args']}
            calls.append(declaration)
            declarations.append(declaration)
            executions.extend([
                {'phase': 'start', 'call_id': call['call_id'], 'name': call['tool'], 'arguments': call['args']},
                {'phase': 'end', 'call_id': call['call_id'], 'name': call['tool'],
                 'result': call['output'], 'is_error': call.get('failed', False)}])
            interactions.append({'tool_call_id': call['call_id'], 'tool_name': call['tool'],
                'tool_arguments': call['args'], 'assistant_message': turn.get('thought', ''),
                'tool_result': call['output'], 'is_error': call.get('failed', False)})
            results.append(json.dumps(call['output'], ensure_ascii=False))
        steps.append({'text': turn.get('thought', ''), 'tool_calls': calls, 'tool_results': results})
    if output.get('answer'):
        steps.append({'text': output['answer'], 'tool_calls': [], 'tool_results': []})
    return {'schema_version': CASE_SCHEMA_VERSION, 'benchmark': benchmark,
            'question_id': output['task_id'], 'final_report': output.get('answer', ''),
            'rollout_status': {'completed': 'FINISHED', 'timeout': 'TRUNCATED', 'failed': 'ABORTED'}[output['state']],
            'tools': output.get('allowed_tools', []),
            'trajectory': {'tool_calls': declarations, 'tool_executions': executions,
                           'interactions': interactions, 'assistant_steps': steps}}


def main():
    parser = argparse.ArgumentParser(description='Adapt one example harness result; no judging')
    parser.add_argument('--input', required=True)
    parser.add_argument('--benchmark', required=True)
    parser.add_argument('--case', required=True)
    args = parser.parse_args()
    case = adapt_output(json.loads(Path(args.input).read_text()), args.benchmark)
    target = Path(args.case)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(case, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
