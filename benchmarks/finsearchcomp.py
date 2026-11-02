"""Native T2/T3 shared binary harness; each track has its own registry entry."""
from ..common import JudgeSession, answer_result, json_file, text_value
from ..config import EvaluationError, resource_path
from ..components import AnswerEvaluator


class FinSearchEvaluator(AnswerEvaluator):
    def __init__(self, track: str):
        self.track = track

    def score(self, case, report, trajectory, cfg):
        row = case.get('row', {})
        if not all(key in row for key in ('judge_system_prompt', 'judge_prompt_template', 'response_reference', 'prompt')):
            path = resource_path(cfg, 'dataset_file', case.get('dataset_file'))
            if not path:
                raise EvaluationError(f'{case["benchmark"]} requires its native dataset row or dataset_file')
            records = json_file(path)
            if not isinstance(records, list):
                raise EvaluationError('FinSearchComp dataset must be a record list')
            matches = [r for r in records if isinstance(r, dict) and self.track in str(r.get('prompt_id', ''))
                       and (str(case.get('question_id', '')) in [str(x) for x in r.get('prompt_id', [])]
                            or r.get('prompt', '').strip() == str(case.get('objective', '')).strip())]
            if len(matches) != 1:
                raise EvaluationError(f'{self.track} requires exactly one matching native dataset record; found {len(matches)}')
            row = matches[0]
        identities = str(row.get('prompt_id', ''))
        for other in {'T1', 'T2', 'T3'} - {self.track}:
            if f'({other})' in identities:
                raise EvaluationError(f'{self.track} cannot grade a {other} record')
        system = text_value(row.get('judge_system_prompt'), 'judge_system_prompt')
        template = text_value(row.get('judge_prompt_template'), 'judge_prompt_template')
        try:
            user = template.format(prompt=row['prompt'], response_reference=row['response_reference'], response=report)
        except (KeyError, ValueError) as exc:
            raise EvaluationError(f'invalid native FinSearch judge template: {exc}') from exc
        system += '\nReturn a JSON object with integer answer_score (0 or 1). Grade the final answer, not a generic report schema.'
        def validate(value):
            if type(value.get('answer_score')) is not int or value['answer_score'] not in (0, 1):
                raise EvaluationError('FinSearchComp answer_score must be integer 0 or 1')
            return value
        session = JudgeSession(cfg)
        value = session.require(session.ask(system, user, validate)) if report else {'answer_score': 0}
        return answer_result(value['answer_score'], cfg, session, 'binary_final_answer', track=self.track, judgment=value)
