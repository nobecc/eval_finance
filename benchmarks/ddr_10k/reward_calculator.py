"""DDR-10K checklist report scoring and message-wise trajectory attribution."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from ...common import BenchmarkScore, JudgeSession, component
from ...components import AnswerEvaluator
from ...config import EvaluationError
from .inputs import checklist, _aggregate
from .judge import CHAT_SYSTEM, MESSAGE_SYSTEM, validate_quality


class DDR10kEvaluator(AnswerEvaluator):
    component_ranges = {'answer_quality': (-1.0, 1.0), 'trajectory_evidence': (-1.0, 1.0)}

    def score(self, case: dict[str, Any], report: str, trajectory: dict[str, Any], cfg: dict[str, Any]) -> BenchmarkScore:
        pairs, source = checklist(case, cfg)
        weights = cfg['reward']['weights']
        final_weight = weights['answer_quality']
        step_weight = weights['trajectory_evidence']
        session = JudgeSession(cfg)
        finals, messages, jobs = [], [], []
        for pair in pairs:
            if final_weight:
                if report:
                    user = f"Candidate Report:\n{report}\n\nQuestion: {pair['question']}\n\nGround Truth Answer: {pair['answer']}"
                    jobs.append(('final', pair, lambda user=user: session.ask(CHAT_SYSTEM, user, validate_quality)))
                else:
                    finals.append({'context_quality': 'INSUFFICIENT_INFO', 'evidence_text': '',
                                   'reasoning': 'No final report', 'question': pair['question']})
            if step_weight:
                if trajectory['messages']:
                    user = f"Numbered Messages:\n{trajectory['numbered_context']}\n\nQuestion: {pair['question']}\n\nGround Truth Answer: {pair['answer']}"
                    jobs.append(('message', pair, lambda user=user: session.ask(MESSAGE_SYSTEM, user,
                        lambda value: validate_quality(value, len(trajectory['messages'])))))
                else:
                    messages.append({'context_quality': 'INSUFFICIENT_INFO', 'evidence_text': '',
                        'reasoning': 'No completed tool interactions', 'supporting_message_indices': [],
                        'contradicting_message_indices': [], 'question': pair['question']})
        if jobs:
            with ThreadPoolExecutor(max_workers=session.settings['concurrency']) as pool:
                futures = [(side, pair, pool.submit(job)) for side, pair, job in jobs]
                for side, pair, future in futures:
                    value = {**future.result(), 'question': pair['question']}
                    (finals if side == 'final' else messages).append(value)
        judge = session.info(rubric_source=source)
        failed_sides = {name for name, values in [('answer_quality', finals), ('trajectory_evidence', messages)]
                        if values and all(x.get('context_quality') == 'API_FAILED' for x in values)}
        components, metrics = {}, {}
        if final_weight:
            final_score, final_detail = _aggregate(finals)
            components['answer_quality'] = component(final_score, final_weight, metric='ddr_answer_checklist')
            metrics['answer_checklist'] = {'score': final_score, **final_detail}
        if step_weight:
            step_score, step_detail = _aggregate(messages)
            step_detail.update({'message_count': len(trajectory['messages']),
                'tool_call_ids': trajectory['tool_call_ids'],
                'message_tool_call_ids': trajectory['message_tool_call_ids'],
                'context_source': trajectory['context_source'],
                'truncated_message_indices': trajectory['truncated_message_indices'],
                'message_attribution': [
                    {'message_index': i, 'tool_call_id': call_id,
                     'tool_call_ids': trajectory['message_tool_call_ids'][i],
                     'supporting_checklist_indices': [j for j, value in enumerate(messages) if i in value.get('supporting_message_indices', [])],
                     'contradicting_checklist_indices': [j for j, value in enumerate(messages) if i in value.get('contradicting_message_indices', [])]}
                    for i, call_id in enumerate(trajectory['tool_call_ids'])]})
            components['trajectory_evidence'] = component(step_score, step_weight, metric='ddr_message_wise_checklist')
            metrics['trajectory_checklist'] = {'score': step_score, **step_detail}
        warnings = ['partial judge failures retained in the denominator with zero contribution'] if session.failed else []
        if failed_sides:
            for name in failed_sides:
                components[name].update(status='failed', score=None)
                metric = 'answer_checklist' if name == 'answer_quality' else 'trajectory_checklist'
                metrics[metric]['score'] = None
            error = EvaluationError('all DDR judgments failed for ' + ', '.join(sorted(failed_sides)), judge_info=judge)
            error.components, error.metrics = components, metrics
            raise error
        return BenchmarkScore(components, judge, warnings, metrics)


EVALUATOR = DDR10kEvaluator()
