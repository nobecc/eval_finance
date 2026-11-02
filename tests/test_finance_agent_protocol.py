"""FinanceAgent judge IDs must match native checks without changing scores."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from eval_finance.config import EvaluationError
from eval_finance.judge import parse_json
from eval_finance.reward_calculator import FreeTextRewardCalculator
from eval_finance.validation import indexed


def reply(value):
    return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop',
        'message': {'content': json.dumps(value)}}]}).encode())


class FinanceAgentProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name)/'evaluation.yaml'
        self.config.write_text(yaml.safe_dump({
            'judge': {'model': 'fixture', 'base_url': 'http://127.0.0.1:1/v1',
                      'api_key': '', 'max_retries': 1},
            'reward': {'weights': {'answer_quality': 1, 'tool_protocol': 0}}}))
        self.calculator = FreeTextRewardCalculator(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def case(self, version, count=9):
        operator = 'finance_agent_v2_operator' if version == 'v2' else 'correctness'
        return {'benchmark': 'finance_agent_bench_'+version, 'final_report': 'Candidate answer',
                'row': {'Question': 'Native question', 'Rubric': [
                    {'operator': operator, 'criteria': 'Native check '+str(index),
                     **({'modifiers': {'severity': index+1, 'category': 'must_pass'}}
                        if version == 'v2' else {})} for index in range(count)]}}

    def test_every_native_check_has_explicit_id_and_matching_response_schema(self):
        for version, key, kind in [('v1_1', 'label', 'boolean'), ('v2', 'score', 'number')]:
            with self.subTest(version=version):
                case = self.case(version)
                before = copy.deepcopy(case)
                judgments = {str(index): {key: True if key == 'label' else 1.0}
                             for index in range(9)}
                def ask(system, user, validator):
                    rubric = user.split('Rubric by zero-based criterion ID:\n', 1)[1].split('\n\n', 1)[0]
                    schema = json.loads(user.split('Required response JSON Schema:\n', 1)[1])
                    ids = [str(index) for index in range(9)]
                    self.assertEqual(json.loads(rubric), dict(zip(ids, case['row']['Rubric'])))
                    self.assertEqual(schema['required'], ids)
                    self.assertEqual(list(schema['properties']), ids)
                    self.assertFalse(schema['additionalProperties'])
                    self.assertEqual(schema['properties']['0']['properties'][key]['type'], kind)
                    self.assertIn('starting at "0"', user)
                    return {**validator(judgments), '_attempts': 1}
                with patch('eval_finance.common.JudgeClient') as client:
                    client.return_value.ask.side_effect = ask
                    result = self.calculator.evaluate_case(case)
                self.assertEqual(case, before)
                self.assertEqual(result['components']['answer_quality']['score'], 1)
                self.assertEqual(result['evaluator']['version'], '2.0.1')

    def test_one_based_reply_is_repaired_with_exact_expected_ids(self):
        for version, key, value in [('v1_1', 'label', True), ('v2', 'score', 1.0)]:
            with self.subTest(version=version):
                opener = Mock()
                opener.open.side_effect = [reply({'1': {key: value}, '2': {key: value}}),
                                          reply({'0': {key: value}, '1': {key: value}})]
                with patch('eval_finance.judge.urllib.request.build_opener', return_value=opener), \
                        patch('eval_finance.judge.time.sleep'):
                    result = self.calculator.evaluate_case(self.case(version, 2))
                requests = [json.loads(call.args[0].data) for call in opener.open.call_args_list]
                self.assertEqual(result['judge']['attempts'], 2)
                self.assertEqual(result['components']['answer_quality']['score'], 1)
                self.assertIn("expected_keys=['0', '1']", requests[1]['messages'][-1]['content'])
                self.assertEqual(requests[0]['messages'][1], requests[1]['messages'][1])

    def test_incomplete_scores_stay_failed_after_retry(self):
        opener = Mock()
        opener.open.side_effect = [reply({'0': {'score': 1}}), reply({'0': {'score': 1}})]
        with patch('eval_finance.judge.urllib.request.build_opener', return_value=opener), \
                patch('eval_finance.judge.time.sleep'), self.assertRaises(EvaluationError) as error:
            self.calculator.evaluate_case(self.case('v2', 2))
        self.assertEqual(error.exception.components['answer_quality']['status'], 'failed')
        self.assertIsNone(error.exception.components['answer_quality']['score'])

    def test_zero_score_remains_successful_evaluation(self):
        with patch('eval_finance.common.JudgeClient') as client:
            client.return_value.ask.side_effect = lambda system, user, validator: {
                **validator({'0': {'score': 0}}), '_attempts': 1}
            result = self.calculator.evaluate_case(self.case('v2', 1))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['raw_score'], 0)
        self.assertEqual(result['normalized_score_10'], 0)

    def test_wrapped_missing_extra_and_one_based_ids_are_not_silently_mapped(self):
        valid = {'0': {'score': 1}, '1': {'score': 0.5}}
        for value in [{'judgments': valid}, {'0': valid['0']},
                      {**valid, '2': {'score': 1}}, {'1': valid['0'], '2': valid['1']}]:
            with self.subTest(value=value), self.assertRaisesRegex(EvaluationError, 'expected_keys='):
                indexed(value, 2, 'score')
        for score in [True, '1', -0.1, 1.1, float('nan'), float('inf')]:
            with self.subTest(score=score), self.assertRaises(EvaluationError):
                indexed({'0': {'score': score}}, 1, 'score')
        with self.assertRaises(EvaluationError):
            indexed({'0': {'label': 1}}, 1)

    def test_duplicate_keys_and_oversized_key_diagnostics_are_bounded(self):
        with self.assertRaises(EvaluationError):
            parse_json('{"0":{"score":1},"0":{"score":0}}')
        with self.assertRaises(EvaluationError) as error:
            indexed({'x'*1000: {'score': 1}}, 1, 'score')
        self.assertLess(len(str(error.exception)), 300)


if __name__ == '__main__':
    unittest.main()
