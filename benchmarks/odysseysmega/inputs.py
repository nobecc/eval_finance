"""Fixed-task Markdown (Prompt / Expected Behavior / LLM Judge Rubric) IO."""
import re
from pathlib import Path
from ...common import field_value, text_value
from ...config import resource_path

# The fixed task ships exactly fifteen independently judged checks.
CHECK_IDS = tuple(f'C{number}' for number in range(1, 16))


def section(text, name):
    match = re.search(r'^##\s+' + re.escape(name) + r'\s*\n(.*?)(?=\n##\s|\Z)', text, re.M | re.S)
    return match.group(1).strip() if match else ''


def task_document(case, cfg):
    path = resource_path(cfg, 'task_file', field_value(case, 'task_file'))
    return Path(path).read_text() if path else None


def guide_inputs(case, cfg):
    """(task, expected behavior, rubric); the packaged task.md is the default."""
    text = task_document(case, cfg)
    if text is not None:
        return (text_value(section(text, 'Prompt'), 'OdysseysMega task'),
                section(text, 'Expected Behavior'),
                text_value(section(text, 'LLM Judge Rubric'), 'OdysseysMega LLM Judge Rubric'))
    return (text_value(case.get('objective'), 'OdysseysMega task'),
            field_value(case, 'expected_behavior', default=''),
            text_value(field_value(case, 'llm_judge_rubric', 'rubric'), 'OdysseysMega LLM Judge Rubric'))
