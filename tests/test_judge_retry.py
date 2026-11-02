"""Failure recovery must preserve complete labels and bounded request budgets."""
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from eval_finance.config import EvaluationError, resolve_judge
from eval_finance.judge import JudgeClient


def response(reason='stop', content='{"0": true}'):
    return io.BytesIO(json.dumps({'choices': [{'finish_reason': reason,
        'message': {'content': content}}]}).encode())


def labels(value):
    if set(value) != {'0'} or type(value['0']) is not bool:
        raise EvaluationError('incomplete boolean judgment')
    return value


class JudgeRetryTests(unittest.TestCase):
    def config(self, **options):
        return resolve_judge({'base_url': 'http://127.0.0.1:1/v1', 'model': 'test',
            'api_key': '', 'max_tokens': 16, 'max_tokens_limit': 64,
            'timeout_s': 10, 'total_timeout_s': 30, 'max_retries': 2, **options},
            'finfirst', environment=False)

    def run_client(self, responses, **options):
        opener = Mock()
        opener.open.side_effect = responses
        with patch('eval_finance.judge.urllib.request.build_opener', return_value=opener), \
                patch('eval_finance.judge.time.sleep'):
            result = JudgeClient(self.config(**options)).ask('system', 'original evidence', labels)
        payloads = [json.loads(call.args[0].data) for call in opener.open.call_args_list]
        return result, payloads

    def test_length_grows_budget_without_accepting_partial_labels(self):
        result, payloads = self.run_client([response('length', '{"0": false}'),
                                           response('length', '{"0": false}'), response()])
        self.assertTrue(result['0'])
        self.assertEqual([p['max_tokens'] for p in payloads], [16, 32, 64])
        self.assertEqual(result['_attempts'], 3)
        timeouts = [v['timeout_s'] for v in result['_attempt_history']]
        self.assertGreater(timeouts[1], timeouts[0])
        self.assertLessEqual(timeouts[2], 30)
        self.assertEqual([v.get('finish_reason') for v in result['_attempt_history']], ['length', 'length', 'stop'])
        self.assertTrue(all(p['messages'][1]['content'] == 'original evidence' for p in payloads))

    def test_length_at_cap_fails_without_identical_retries(self):
        result, payloads = self.run_client([response('length')], max_tokens_limit=16)
        self.assertEqual(result['context_quality'], 'API_FAILED')
        self.assertEqual(result['_attempts'], 1)
        self.assertNotIn('0', result)
        self.assertEqual(len(payloads), 1)

    def test_timeout_then_length_still_has_budget_for_growth(self):
        result,payloads=self.run_client([TimeoutError('socket stalled'),response('length'),response()])
        self.assertTrue(result['0'])
        self.assertEqual([p['max_tokens'] for p in payloads],[16,16,32])
        self.assertEqual(result['_attempts'],3)

    def test_response_diagnostics_record_only_sizes_and_token_usage(self):
        body={'choices':[{'finish_reason':'stop','message':{'content':'{"0":true}',
              'reasoning_content':'short reasoning'}}],
              'usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20,'private_metadata':'not recorded'}}
        result,_=self.run_client([io.BytesIO(json.dumps(body).encode())])
        history=result['_attempt_history'][0]
        self.assertEqual(history['content_chars'],10)
        self.assertEqual(history['reasoning_chars'],15)
        self.assertEqual(history['usage'],{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20})
        self.assertNotIn('short reasoning',json.dumps(history))
        self.assertNotIn('private_metadata',json.dumps(history))

    def test_transport_retry_preserves_budget_and_evidence(self):
        result, payloads = self.run_client([TimeoutError('socket stalled'), response()])
        self.assertTrue(result['0'])
        self.assertEqual(payloads[0], payloads[1])

    def test_permanent_failure_and_filter_do_not_retry(self):
        for failure in [urllib.error.HTTPError('http://local', 401, 'unauthorized', {}, None),
                        response('content_filter')]:
            with self.subTest(failure=failure):
                result, payloads = self.run_client([failure])
                self.assertEqual(result['context_quality'], 'API_FAILED')
                self.assertEqual(len(payloads), 1)

    def test_invalid_labels_get_compact_repair_without_budget_growth(self):
        result, payloads = self.run_client([response(content='{"1": true}'), response()])
        self.assertTrue(result['0'])
        self.assertEqual([p['max_tokens'] for p in payloads], [16, 16])
        self.assertEqual(payloads[1]['messages'][1]['content'], 'original evidence')

    def test_total_timeout_limits_attempts_and_request_timeout(self):
        opener = Mock()
        with patch('eval_finance.judge.time.monotonic', return_value=0) as clock:
            def timeout(*args, **kwargs):
                clock.return_value = 5
                raise TimeoutError('request took remaining budget')
            opener.open.side_effect = timeout
            with patch('eval_finance.judge.urllib.request.build_opener', return_value=opener), \
                    patch('eval_finance.judge.time.sleep'):
                result = JudgeClient(self.config(total_timeout_s=4)).ask('system', 'evidence', labels)
        self.assertEqual(result['_attempts'], 1)
        self.assertEqual(result['error'], 'judge total timeout exhausted')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 4)

    def test_optional_budget_validation_and_environment(self):
        with self.assertRaises(EvaluationError):
            self.config(max_tokens_limit=15)
        with patch.dict('os.environ', {'EVAL_FINANCE_JUDGE_MAX_TOKENS_LIMIT': '128',
                                     'EVAL_FINANCE_JUDGE_TOTAL_TIMEOUT_S': '60'}):
            cfg = resolve_judge({'model': 'test', 'base_url': 'http://local', 'max_tokens': 16}, 'finfirst')
        self.assertEqual(cfg['max_tokens_limit'], 128)
        self.assertEqual(cfg['total_timeout_s'], 60)


if __name__ == '__main__':
    unittest.main()
