"""Explicit benchmark dispatch. Registered tasks never use generic fallback."""
from importlib import import_module
from .config import EvaluationError

EVALUATORS = {
    name: f'eval_finance.benchmarks.{name}.reward_calculator'
    for name in ('ddr_10k', 'finfirst', 'finance_agent_bench_v1_1', 'finance_agent_bench_v2',
                 'finsearchcomp_t2', 'finsearchcomp_t3', 'dataclawbench', 'finance_complex_qa',
                 'frontier_finance')
}


def get_evaluator(benchmark: str):
    module = EVALUATORS.get(benchmark)
    if benchmark == 'generic':
        module = 'eval_finance.benchmarks.generic.reward_calculator'
    if module is None:
        raise EvaluationError(f'no evaluator registered for benchmark {benchmark!r}')
    return import_module(module).EVALUATOR
