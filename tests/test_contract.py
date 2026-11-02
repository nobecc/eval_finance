"""A second harness and standalone JSON consume the same scoring contract."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from eval_finance import BenchmarkRewardCalculator, CaseInputs, EvaluationError
from eval_finance.cli import main
from eval_finance.config import load_config, resolve_judge, resource_path
from eval_finance.examples.custom_harness import adapt_output
from eval_finance.trajectory import load_steps


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'evaluation.yaml'
        self.output = json.loads((Path(__file__).parents[1] / 'examples/custom_output.json').read_text())
        self.case = adapt_output(self.output, 'ddr_10k')
        self.configure(tool_protocol=1)

    def tearDown(self):
        self.temp.cleanup()

    def configure(self, **weights):
        names = ['answer_quality', 'trajectory_evidence', 'tool_protocol',
                 'goal_progress', 'step_efficiency', 'milestone_timeliness']
        self.config.write_text(yaml.safe_dump({'judge': {'model': 'test', 'max_retries': 0},
            'reward': {'weights': {**dict.fromkeys(names, 0), **weights}, 'max_message_chars': 0}}))
        self.calc = BenchmarkRewardCalculator(self.config)

    def test_second_harness_scores_every_registered_benchmark_without_native_io(self):
        from eval_finance.registry import EVALUATORS
        for name in EVALUATORS:
            case = {**self.case, 'benchmark': name, 'row_file': '/missing/row',
                    'final_report': {'unused': True}}
            with self.subTest(name=name), patch('eval_finance.common.JudgeClient') as judge:
                document = self.calc.evaluate_case(case)
            judge.assert_not_called()
            self.assertEqual(document['schema_version'], 'eval_finance.reward.v1')
            self.assertNotIn('official', document)
            self.assertEqual(document['raw_score'], 1)
            self.assertEqual(document['evaluator']['id'], f'eval_finance.{name}')

    def test_evidence_only_uses_normalized_interactions_without_protocol_or_report(self):
        self.configure(trajectory_evidence=0.4)
        case = {**self.case, 'checklist': [{'question': 'Revenue?', 'answer': '100'}],
                'final_report': {'unused': True}}
        case['trajectory'] = {'message_wise_insights': [{
                                  'text': 'Revenue was 100.', 'tool_call_ids': ['c1']}],
                              'interactions': self.case['trajectory']['interactions'],
                              'tool_calls': 'unused', 'assistant_steps': 'unused'}
        def ask(system, user, validator):
            self.assertIn('Revenue was 100.', user)
            self.assertNotIn('Observed result:', user)
            return {**validator({'context_quality': 'CORRECT_INFO', 'evidence_text': '100',
                'reasoning': 'Matches reference', 'supporting_message_indices': [0],
                'contradicting_message_indices': []}), '_attempts': 1}
        with patch('eval_finance.common.JudgeClient') as judge:
            judge.return_value.ask.side_effect = ask
            document = self.calc.evaluate_case(case)
        self.assertEqual(document['raw_score'], 0.4)
        self.assertEqual(document['metrics']['trajectory_checklist']['tool_call_ids'], ['c1'])
        self.assertEqual(document['metrics']['trajectory_checklist']['context_source'], 'message_wise_insights')

    def test_dataclaw_efficiency_uses_assistant_steps_from_another_harness(self):
        self.configure(step_efficiency=1)
        case = {**self.case, 'benchmark': 'dataclawbench', 'outcome_score': 1,
                'process_gold': {'steps': ['Retrieve', 'Answer'], 'steps_num': 2}}
        with patch('eval_finance.common.JudgeClient') as judge:
            document = self.calc.evaluate_case(case)
        judge.assert_not_called()
        self.assertEqual(document['metrics']['assistant_step_count'], 2)
        self.assertEqual(document['metrics']['execution_efficiency'], 1)
        self.assertEqual(document['raw_score'], 0.5)

    def test_dataclaw_progress_uses_normalized_steps_and_native_milestones(self):
        self.configure(goal_progress=1, milestone_timeliness=0.4)
        case = {**self.case, 'benchmark': 'dataclawbench', 'outcome_score': 0.5,
                'process_gold': {'steps': ['Retrieve', 'Answer'], 'steps_num': 2, 'milestone': {'revenue': 1}}}
        with patch('eval_finance.common.JudgeClient') as judge:
            judge.return_value.ask.side_effect = lambda system, user, validator: {
                **validator({'milestones': [{'key': 'revenue', 'achieved': True, 'first_step': 1}]}), '_attempts': 1}
            document = self.calc.evaluate_case(case)
        self.assertEqual(document['raw_score'], 1.4)
        self.assertEqual(document['judge']['calls'], 1)

    def test_answer_only_skips_bad_trajectory_and_preserves_report_verbatim(self):
        self.configure(answer_quality=1)
        report = '  FINISH: This is literal answer content.\n'
        case = {**self.case, 'benchmark': 'finance_complex_qa', 'final_report': report,
                'trajectory': 'unused', 'row': {'question': 'Q', 'gold': 'A'}}
        with patch('eval_finance.common.JudgeClient') as judge:
            judge.return_value.ask.side_effect = lambda system, user, validator: {**validator({'score': 1}), '_attempts': 1}
            document = self.calc.evaluate_case(case)
        self.assertIn(report, judge.return_value.ask.call_args.args[1])
        self.assertEqual(document['raw_score'], 1)

    def test_standard_trajectory_artifact_is_loaded_and_cached(self):
        path = self.root / 'trajectory.json'
        path.write_text(json.dumps(self.case['trajectory']))
        case = {key: value for key, value in self.case.items() if key != 'trajectory'}
        case['artifacts'] = {'trajectory': str(path)}
        inputs = CaseInputs(case)
        self.assertEqual(self.calc.evaluate_case(case, inputs=inputs)['raw_score'], 1)
        path.unlink()
        self.assertEqual(self.calc.evaluate_case(case, inputs=inputs)['raw_score'], 1)

    def test_inapplicable_process_branch_skips_trajectory_resource(self):
        self.configure(goal_progress=1)
        case = {'benchmark': 'dataclawbench', 'outcome_score': 1,
                'artifacts': {'trajectory': '/missing/trajectory.json'}, 'gold_file': '/missing/gold'}
        document = self.calc.evaluate_case(case)
        self.assertEqual(document['components']['goal_progress']['status'], 'not_applicable')

    def test_protocol_preserves_dangling_duplicate_orphan_and_execution_error(self):
        cases = []
        dangling = copy.deepcopy(self.case)
        dangling['trajectory']['tool_executions'].pop()
        cases.append((dangling, 0))
        duplicate = copy.deepcopy(self.case)
        duplicate['trajectory']['tool_calls'].append(duplicate['trajectory']['tool_calls'][0])
        cases.append((duplicate, 0))
        orphan = copy.deepcopy(self.case)
        orphan['trajectory']['tool_calls'] = []
        cases.append((orphan, 0))
        error = copy.deepcopy(self.case)
        error['trajectory']['tool_executions'][-1]['is_error'] = True
        cases.append((error, 1))
        for case, score in cases:
            with self.subTest(score=score):
                document = self.calc.evaluate_case(case)
                self.assertEqual(document['raw_score'], score)
        self.assertEqual(document['metrics']['tool_protocol']['execution_errors'], 1)

    def test_null_tool_result_counts_as_present_and_absent_result_is_invalid(self):
        case = copy.deepcopy(self.case)
        case['trajectory']['tool_executions'][-1]['result'] = None
        self.assertEqual(self.calc.evaluate_case(case)['raw_score'], 1)
        del case['trajectory']['tool_executions'][-1]['result']
        self.assertEqual(self.calc.evaluate_case(case)['raw_score'], 0)

    def test_invalid_schema_or_active_trajectory_structure_fails(self):
        for case in [{**self.case, 'schema_version': 'eval_finance.case.v2'},
                     {**self.case, 'trajectory': []},
                     {**self.case, 'trajectory': {'tool_calls': ['bad']}},
                     {**self.case, 'trajectory': {'tool_executions': [{'phase': 'bad'}]}}]:
            with self.subTest(case=case), self.assertRaises(EvaluationError):
                self.calc.evaluate_case(case)

    def test_new_environment_names_override_legacy_aliases(self):
        with patch.dict(os.environ, {'EVAL_FINANCE_JUDGE_MODEL': 'new',
                                     'PI_FINANCE_EVAL_JUDGE_MODEL': 'old',
                                     'EVAL_FINANCE_CONFIG': str(self.config),
                                     'PI_FINANCE_EVAL_CONFIG': '/missing/config',
                                     'EVAL_FINANCE_QA_FILE': str(self.root / 'new-qa'),
                                     'PI_FINANCE_EVAL_QA_FILE': '/old-qa'}, clear=True):
            cfg = load_config(None, 'ddr_10k')
            self.assertEqual(resolve_judge(cfg['judge'], 'ddr_10k')['model'], 'new')
            self.assertEqual(resource_path(cfg, 'qa_file'), str(self.root / 'new-qa'))

    def test_cli_reports_input_failure_with_neutral_schema(self):
        case = self.root / 'case.json'
        reward = self.root / 'reward.json'
        case.write_text(json.dumps({**self.case, 'trajectory': []}))
        with patch('sys.stdout'), patch('sys.stderr'):
            code = main(['--case', str(case), '--config', str(self.config), '--output', str(reward)])
        document = json.loads(reward.read_text())
        self.assertEqual(code, 1)
        self.assertEqual(document['schema_version'], 'eval_finance.reward.v1')
        self.assertNotIn('official', document)
        self.assertIsNone(document['raw_score'])
        self.assertEqual(document['components']['tool_protocol']['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
