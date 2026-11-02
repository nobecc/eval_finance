"""Reward vocabulary; benchmarks declare which components they implement."""
from __future__ import annotations

COMPONENT_NAMES = (
    'answer_quality', 'trajectory_evidence', 'tool_protocol',
    'goal_progress', 'step_efficiency', 'milestone_timeliness',
)


class AnswerEvaluator:
    """Default capabilities for native final-answer evaluators."""

    version = '2.0.0'
    component_ranges = {'answer_quality': (0.0, 1.0)}

    def reward_bounds(self, weights):
        ranges = {**self.component_ranges, 'tool_protocol': (0.0, 1.0)}
        return (
            sum(weights[name] * bounds[0] for name, bounds in ranges.items()),
            sum(weights[name] * bounds[1] for name, bounds in ranges.items()),
        )
