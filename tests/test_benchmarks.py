"""Benchmark protocol regression tests with their native reference shapes."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import yaml

from eval_finance.config import EvaluationError, load_config, resource_path
from eval_finance.registry import EVALUATORS, get_evaluator
from eval_finance.reward_calculator import FreeTextRewardCalculator
from eval_finance.benchmarks.finfirst.reward_calculator import atomic_rubrics
from eval_finance.benchmarks.frontier_finance.reward_calculator import majority_vote


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pi-benchmark-test-')
        self.root = Path(self.temp.name)
        self.config = self.root/'config.yaml'
        self.config.write_text(yaml.safe_dump({'fallback_judge': True,
            'judge': {'model':'test','base_url':'http://127.0.0.1:1/v1','max_retries':0}}))
        self.calc = FreeTextRewardCalculator(self.config)
        self.base = {'objective':'The original question','final_report':'A free-text answer.','tools':[],
                     'rollout_status':'FINISHED','artifacts':{}}

    def tearDown(self):
        self.temp.cleanup()

    def evaluate(self, benchmark, response, **data):
        def ask(system,user,validator):
            return {**validator(copy.deepcopy(response)),'_attempts':1}
        with patch('eval_finance.common.JudgeClient') as client:
            client.return_value.ask.side_effect = ask
            doc = self.calc.evaluate_case({**self.base,'benchmark':benchmark,**data})
            requests = client.return_value.ask.call_args_list
        self.assertEqual(doc['schema_version'],'eval_finance.reward.v1')
        self.assertNotIn('trajectory_evidence', doc['components'])
        self.assertNotIn('final_report_accuracy', doc['components'])
        self.assertEqual(doc['components']['tool_protocol']['detail']['metric'],'valid_tool_call_ratio')
        return doc,requests

    def test_registry_has_nine_concrete_entries_and_unknown_never_falls_back(self):
        self.assertEqual(len(EVALUATORS),9)
        for name in EVALUATORS:
            self.assertTrue(callable(get_evaluator(name).score))
            self.assertTrue((Path(__file__).resolve().parents[1]/'benchmarks'/name/'config.yaml').is_file())
        with self.assertRaises(EvaluationError):
            self.calc.evaluate_case({**self.base,'benchmark':'unknown','reference_answer':'A'})

    def test_registered_benchmarks_reject_ddr_checklist_as_native_inputs(self):
        # ddr_10k owns the checklist natively. Every other scorer must
        # demand its native inputs rather than accepting a DDR checklist.
        for name in set(EVALUATORS)-{'ddr_10k'}:
            case={**self.base,'benchmark':name,'checklist':[{'question':'Q','answer':'A'}]}
            # Each registered scorer needs its own rubric/reference protocol.
            with self.subTest(name=name), patch('eval_finance.common.JudgeClient') as client:
                with self.assertRaises(EvaluationError):self.calc.evaluate_case(case)
                client.assert_not_called()

    def test_finsearch_t2_and_t3_use_supplied_binary_judge_and_tolerance(self):
        for track in ['T2','T3']:
            row={'prompt_id':[f'({track})id'],'prompt':'Original lookup question',
                 'response_reference':'12.3; tolerate 1% error','judge_system_prompt':'native_binary',
                 'judge_prompt_template':'{prompt}\nREFERENCE: {response_reference}\nRESPONSE: {response}'}
            doc,requests=self.evaluate('finsearchcomp_'+track.lower(),{'answer_score':0},row=row)
            self.assertEqual(doc['components']['answer_quality']['score'],0)
            self.assertEqual(doc['judge']['calls'],1)
            self.assertIn('tolerate 1%',requests[0].args[1])
            self.assertNotIn('Numbered Messages',requests[0].args[1])

    def test_finsearch_wrong_track_and_nonbinary_grade_are_rejected(self):
        row={'prompt_id':['(T3)id'],'prompt':'Q','response_reference':'A',
             'judge_system_prompt':'native','judge_prompt_template':'{prompt}{response_reference}{response}'}
        with self.assertRaises(EvaluationError):
            self.evaluate('finsearchcomp_t2',{'answer_score':1},row=row)
        with self.assertRaises(EvaluationError):
            self.evaluate('finsearchcomp_t3',{'answer_score':0.5},row=row)

    def test_finsearch_dataset_lookup_and_ambiguous_id(self):
        dataset=self.root/'data.json'
        row={'prompt_id':['(T2)id'],'prompt':'The original question','response_reference':'A',
             'judge_system_prompt':'native','judge_prompt_template':'{prompt}{response_reference}{response}'}
        dataset.write_text(json.dumps([row]))
        doc,_=self.evaluate('finsearchcomp_t2',{'answer_score':1},dataset_file=str(dataset))
        self.assertEqual(doc['components']['answer_quality']['score'],1)
        dataset.write_text(json.dumps([row,row]))
        with self.assertRaises(EvaluationError):
            self.evaluate('finsearchcomp_t2',{'answer_score':1},dataset_file=str(dataset))

    def test_finance_complex_qa_uses_its_discrete_native_grades(self):
        for value in [0,0.5,1]:
            doc,_=self.evaluate('finance_complex_qa',{'score':value},row={'gold':'Reference answer'})
            self.assertEqual(doc['components']['answer_quality']['score'],value)
        with self.assertRaises(EvaluationError):
            self.evaluate('finance_complex_qa',{'score':0.25},reference_answer='Reference')

    def test_dataclaw_preserves_single_and_multi_answer_rubric_total(self):
        doc,requests=self.evaluate('dataclawbench',{'scores':{'a':1,'b':0},'total':0.5,'notes':'One matches'},
            llm_judge_rubric='Two-answer task: one point per semantic match; total is mean.')
        self.assertEqual(doc['components']['answer_quality']['score'],0.5)
        self.assertIn('Two-answer task',requests[0].args[1])

    def test_dataclaw_reads_native_markdown_sections(self):
        task=self.root/'task.md'
        task.write_text('## Prompt\nOriginal task\n\n## Expected Behavior\nCompute.\n\n## LLM Judge Rubric\nGold: 10. Return match and total.\n')
        doc,requests=self.evaluate('dataclawbench',{'scores':{'match':1},'total':1},task_file=str(task))
        self.assertEqual(doc['components']['answer_quality']['score'],1)
        self.assertIn('Gold: 10',requests[0].args[1])

    def test_finance_agent_v11_uses_operator_all_pass_not_partial_relevance(self):
        rubric=[{'operator':'correctness','criteria':'Required fact'},
                {'operator':'contradiction','criteria':'Authoritative reference'}]
        doc,_=self.evaluate('finance_agent_bench_v1_1',{'0':{'label':False},'1':{'label':True}},row={'Rubric':json.dumps(rubric)})
        self.assertEqual(doc['components']['answer_quality']['score'],0)
        self.assertEqual(doc['metrics']['criterion_pass_rate'],0.5)

    def test_finance_agent_v2_severity_weighting_and_dealbreaker(self):
        rubric=[{'operator':'finance_agent_v2_operator','criteria':'Essential','modifiers':{'severity':3,'category':'must_pass'}},
                {'operator':'finance_agent_v2_operator','criteria':'Peripheral','modifiers':{'severity':1}}]
        doc,_=self.evaluate('finance_agent_bench_v2',{'0':{'score':1},'1':{'score':0}},rubric=rubric)
        self.assertEqual(doc['components']['answer_quality']['score'],0.75)
        failed,_=self.evaluate('finance_agent_bench_v2',{'0':{'score':0.5},'1':{'score':1}},rubric=rubric)
        self.assertEqual(failed['components']['answer_quality']['score'],0)
        self.assertTrue(failed['metrics']['dealbreaker_failed'])

    def test_finance_agent_versions_reject_each_others_operators(self):
        with self.assertRaises(EvaluationError):
            self.evaluate('finance_agent_bench_v2',{},rubric=[{'operator':'correctness','criteria':'A'}])

    def test_finfirst_uses_expert_weights_groups_and_strict_pass(self):
        rubrics=[{'criterion':'Retrieved fact','weight':80,'group':'raw_information'},
                 {'criterion':'Verified source','weight':20,'group':'source_verification'}]
        doc,_=self.evaluate('finfirst',{'0':{'label':True},'1':{'label':False}},rubrics=rubrics)
        detail=doc['metrics']
        self.assertEqual(doc['components']['answer_quality']['score'],0.8)
        self.assertEqual(detail['atomic_pass_rate'],0.5)
        self.assertFalse(detail['strict_pass'])
        self.assertEqual(detail['group_scores']['source_verification'],0)
        self.assertNotIn('source_verification',doc['components'])

    def test_finfirst_boolean_only_native_decisions_preserve_weights(self):
        rubrics=[{'criterion':'Retrieved fact','weight':80,'group':'raw_information'},
                 {'criterion':'Verified source','weight':20,'group':'source_verification'}]
        doc,_=self.evaluate('finfirst',{'0':True,'1':False},rubrics=rubrics)
        self.assertEqual(doc['metrics']['labels'],[True,False])
        self.assertEqual(doc['metrics']['weighted_rubric_score'],0.8)
        self.assertEqual(doc['metrics']['boolean_only_criteria'],[0,1])
        self.assertIsNone(doc['metrics']['judgments']['0']['reason'])
        self.assertTrue(doc['warnings'])

    def test_finfirst_rejects_missing_indices_and_non_boolean_decisions(self):
        rubrics=[{'criterion':'Required fact','weight':100,'group':'raw_information'}]
        for response in ({'1':True},{'0':1},{'0':'true'}):
            with self.subTest(response=response), self.assertRaises(EvaluationError):
                self.evaluate('finfirst',response,rubrics=rubrics)

    def test_finfirst_parses_annotated_chinese_native_rubric(self):
        text='1、取得所需数值，80分；（原始数据查询）\n2、计算正确答案，20分（计算和答案）'
        items=atomic_rubrics({'row':{'rubric_annotated':text}})
        self.assertEqual([x['weight'] for x in items],[80,20])
        self.assertEqual([x['group'] for x in items],['raw_information','computation_answer'])
        with self.assertRaises(EvaluationError):atomic_rubrics({'rubric':'1、只有一个项目，20分'})

    def test_finfirst_multiline_decimal_is_not_a_criterion_header(self):
        text='"1. Identify a source, 20 points（信源核验）\n2. Calculate the margin\n 42.21% - 31.97% = 10.24 percentage points\n Correct answer, 80 points（计算和答案）"'
        items=atomic_rubrics({'rubric_annotated':text})
        self.assertEqual(len(items),2)
        self.assertEqual([x['weight'] for x in items],[20,80])
        self.assertEqual(items[0]['group'],'source_verification')

    def test_native_question_comes_from_dataset_row_not_runtime_wrapper(self):
        doc,requests=self.evaluate('finance_complex_qa',{'score':1},
            row={'question':['Dataset question'],'gold':'Reference answer'})
        self.assertIn('Dataset question',requests[0].args[1])
        self.assertNotIn(self.base['objective'],requests[0].args[1])

    def test_frontier_date_anchor_majority_and_first_surviving_tie(self):
        panel=[{'model':x} for x in ['first','second','third']]
        config=yaml.safe_load(self.config.read_text());config['frontier_finance']={'judges':panel}
        self.config.write_text(yaml.safe_dump(config))
        replies={'first':{'0':{'label':True},'1':{'label':False}},
                 'second':{'0':{'label':False},'1':{'label':True}},
                 'third':{'context_quality':'API_FAILED','error':'offline','_attempts':1}}
        class PanelJudge:
            def __init__(self,cfg):self.model=cfg['model']
            def ask(self,system,user,validator):
                if self.model=='third':return replies[self.model]
                self_outer.assertIn('2024-12-31',user)
                return {**validator(copy.deepcopy(replies[self.model])),'_attempts':1}
        self_outer=self
        with patch('eval_finance.common.JudgeClient',PanelJudge):
            doc=self.calc.evaluate_case({**self.base,'benchmark':'frontier_finance','query_date':'2024-12-31',
                'rubrics':[{'rubric_text':'A','must_have':True},{'rubric_text':'B','must_have':False}]})
        detail=doc['metrics']
        self.assertEqual(detail['labels'],[True,False])
        self.assertEqual(doc['components']['answer_quality']['score'],0.5)
        self.assertEqual(doc['judge']['failed'],1)
        self.assertEqual(detail['surviving_panel_size'],2)
        self.assertEqual(majority_vote([[True],[False],[True]]),[True])

    def test_frontier_missing_date_and_missing_label_fail_loudly(self):
        with self.assertRaises(EvaluationError):self.evaluate('frontier_finance',{},rubrics=[{'rubric_text':'A'}])
        with self.assertRaises(EvaluationError):
            self.evaluate('frontier_finance',{},query_date='2024-12-31',rubrics=[{'rubric_text':'A'}])

    def test_local_ddr_defaults_and_derived_ranges(self):
        ddr=load_config(self.config,'ddr_10k');qa=load_config(self.config,'finance_complex_qa')
        self.assertEqual(ddr['reward']['weights']['trajectory_evidence'],0.4)
        self.assertEqual(qa['reward']['weights']['trajectory_evidence'],0)
        self.assertEqual(get_evaluator('ddr_10k').reward_bounds(ddr['reward']['weights']),(-1.4,1.5))
        self.assertEqual(get_evaluator('finance_complex_qa').reward_bounds(qa['reward']['weights']),(0,1.1))
        self.assertIsNone(resource_path(ddr,'qa_file'))

    def test_reference_resources_are_explicit_and_config_relative(self):
        resources = {'ddr_10k': 'qa_file', 'finsearchcomp_t2': 'dataset_file',
                     'finsearchcomp_t3': 'dataset_file', 'dataclawbench': 'assets_dir'}
        with patch.dict('os.environ', {}, clear=True):
            for benchmark, key in resources.items():
                with self.subTest(benchmark=benchmark):
                    self.assertIsNone(resource_path(load_config(None, benchmark), key))
                    config = self.root / (benchmark + '.yaml')
                    config.write_text(yaml.safe_dump({benchmark: {key: 'native/resource'}}))
                    cfg = load_config(config, benchmark)
                    self.assertEqual(resource_path(cfg, key), str(self.root / 'native/resource'))


if __name__=='__main__':unittest.main()
