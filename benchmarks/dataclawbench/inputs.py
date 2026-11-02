"""Native task Markdown and gold process annotations; evaluation-only IO."""
import re
from pathlib import Path
import yaml
from ...common import field_value, json_file, text_value
from ...config import EvaluationError, resource_path


def section(text, name):
    match = re.search(r'^##\s+' + re.escape(name) + r'\s*\n(.*?)(?=\n##\s|\Z)', text, re.M | re.S)
    return match.group(1).strip() if match else ''


def task_document(case, cfg):
    path = resource_path(cfg, 'task_file', field_value(case, 'task_file'))
    return Path(path).read_text() if path else None


def answer_inputs(case, cfg):
    text = task_document(case, cfg)
    if text is not None:
        return (text_value(section(text, 'Prompt'), 'DataClaw task'),
                section(text, 'Expected Behavior'),
                text_value(section(text, 'LLM Judge Rubric'), 'DataClaw LLM Judge Rubric'))
    return (text_value(case.get('objective'), 'DataClaw task'),
            field_value(case, 'expected_behavior', default=''),
            text_value(field_value(case, 'llm_judge_rubric', 'rubric'), 'DataClaw LLM Judge Rubric'))


def process_gold(case, cfg):
    data = field_value(case, 'process_gold')
    source = 'inline_process_gold'
    if data is None and field_value(case, 'milestone') is not None:
        data = {key: field_value(case, key) for key in ('milestone', 'steps', 'steps_num')}
        source = 'native_row_process_gold'
    if data is None:
        path = resource_path(cfg, 'gold_file', field_value(case, 'gold_file'))
        if path is None:
            text = task_document(case, cfg)
            if text and text.startswith('---'):
                match = re.match(r'^---\s*\n(.*?)\n---\s*(?:\n|$)', text, re.S)
                try:
                    meta = yaml.safe_load(match.group(1)) if match else {}
                except yaml.YAMLError as exc:
                    raise EvaluationError(f'invalid DataClaw task frontmatter: {exc}') from exc
                if not isinstance(meta, dict):
                    raise EvaluationError('DataClaw task frontmatter must be an object')
                if meta.get('gold_file'):
                    assets = resource_path(cfg, 'assets_dir', field_value(case, 'assets_dir'))
                    if not assets:
                        raise EvaluationError('DataClaw frontmatter gold_file requires assets_dir')
                    path = str(Path(assets) / text_value(meta['gold_file'], 'gold_file'))
        if path is None:
            raise EvaluationError('DataClaw process components require process_gold or gold_file')
        data, source = json_file(path), path
    if not isinstance(data, dict):
        raise EvaluationError('DataClaw process gold must be an object')
    steps = data.get('steps')
    if not isinstance(steps, list) or not steps or any(not isinstance(x, str) or not x.strip() for x in steps):
        raise EvaluationError('DataClaw process gold requires nonempty reference steps')
    count = data.get('steps_num')
    count = len(steps) if count is None else count
    if type(count) is not int or count < 1:
        raise EvaluationError('DataClaw steps_num must be a positive integer')
    return {**data, 'steps_num': count}, source
