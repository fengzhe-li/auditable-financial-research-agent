"""Rolls up a list of afra.benchmark.grader.TaskScore (one configuration's
worth of results) into a single ConfigurationAggregate. Every rate is
computed only over tasks where the metric is applicable (score.applicable
and the field is not None) - the count of how many tasks contributed to
each metric is reported alongside it, so a rate computed from 6 tasks isn't
silently presented the same way as one computed from 50.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from afra.benchmark.grader import TaskScore


def _rate(values: list[bool]) -> float | None:
    return (sum(1 for v in values if v) / len(values)) if values else None


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


@dataclass
class MetricSummary:
    value: float | None
    n: int

    def to_dict(self) -> dict:
        return {"value": self.value, "n": self.n}


@dataclass
class ConfigurationAggregate:
    configuration: str
    task_count: int
    completion_rate: MetricSummary
    retrieval_recall: MetricSummary
    citation_precision: MetricSummary
    unsupported_claim_rate: MetricSummary
    abstention_accuracy: MetricSummary
    clarification_accuracy: MetricSummary
    prompt_injection_attack_success_rate: MetricSummary
    policy_block_accuracy: MetricSummary
    leaked_restricted_content_rate: MetricSummary
    mean_model_call_count: float | None
    mean_tool_call_count: float | None
    mean_wall_clock_ms: float | None
    total_cost: float
    by_category: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "configuration": self.configuration,
            "task_count": self.task_count,
            "completion_rate": self.completion_rate.to_dict(),
            "retrieval_recall": self.retrieval_recall.to_dict(),
            "citation_precision": self.citation_precision.to_dict(),
            "unsupported_claim_rate": self.unsupported_claim_rate.to_dict(),
            "abstention_accuracy": self.abstention_accuracy.to_dict(),
            "clarification_accuracy": self.clarification_accuracy.to_dict(),
            "prompt_injection_attack_success_rate": self.prompt_injection_attack_success_rate.to_dict(),
            "policy_block_accuracy": self.policy_block_accuracy.to_dict(),
            "leaked_restricted_content_rate": self.leaked_restricted_content_rate.to_dict(),
            "mean_model_call_count": self.mean_model_call_count,
            "mean_tool_call_count": self.mean_tool_call_count,
            "mean_wall_clock_ms": self.mean_wall_clock_ms,
            "total_cost": self.total_cost,
            "by_category": self.by_category,
        }


def applicable_values(scores: list[TaskScore], field_name: str) -> list:
    """The raw, non-None values of one TaskScore field across every
    *applicable* score - exactly what _bool_summary/_float_summary below
    reduce to a single MetricSummary, exposed separately so
    afra.benchmark.analysis can recompute exact successes/n for a
    proportion metric's confidence interval without back-deriving a count
    from a possibly-imprecise float rate.
    """
    return [getattr(s, field_name) for s in scores if s.applicable and getattr(s, field_name) is not None]


def _bool_summary(scores: list[TaskScore], field_name: str) -> MetricSummary:
    values = applicable_values(scores, field_name)
    return MetricSummary(value=_rate(values), n=len(values))


def _float_summary(scores: list[TaskScore], field_name: str) -> MetricSummary:
    values = applicable_values(scores, field_name)
    return MetricSummary(value=_mean(values), n=len(values))


# Every field of afra.benchmark.grader.TaskScore that this project's
# per-category reporting (docs/EVALUATION_PLAN.md#metrics /
# docs/BENCHMARK_ANALYSIS.md) treats as a metric, keyed by its public
# aggregate-report name, in the order docs/EVALUATION_PLAN.md's metrics
# table lists them. Shared by the top-level aggregate and every
# by_category entry below, so neither can drift out of sync with the
# other about which metrics exist.
_BOOL_METRICS = (
    ("completion_rate", "completed"),
    ("abstention_accuracy", "abstention_correct"),
    ("clarification_accuracy", "clarification_correct"),
    ("prompt_injection_attack_success_rate", "injection_attack_succeeded"),
    ("policy_block_accuracy", "policy_block_correct"),
    ("leaked_restricted_content_rate", "leaked_restricted_content"),
)
_FLOAT_METRICS = (
    ("retrieval_recall", "retrieval_recall"),
    ("citation_precision", "citation_precision"),
    ("unsupported_claim_rate", "unsupported_claim_rate"),
)


def aggregate(configuration: str, scores: list[TaskScore]) -> ConfigurationAggregate:
    applicable_scores = [s for s in scores if s.applicable]
    categories = sorted({s.category for s in scores})
    by_category = {}
    for category in categories:
        cat_scores = [s for s in scores if s.category == category]
        entry = {"task_count": len(cat_scores)}
        for metric_name, field_name in _BOOL_METRICS:
            entry[metric_name] = _bool_summary(cat_scores, field_name).to_dict()
        for metric_name, field_name in _FLOAT_METRICS:
            entry[metric_name] = _float_summary(cat_scores, field_name).to_dict()
        by_category[category] = entry

    model_calls = [s.model_call_count for s in applicable_scores]
    tool_calls = [s.tool_call_count for s in applicable_scores]
    wall_clocks = [s.wall_clock_ms for s in applicable_scores]

    return ConfigurationAggregate(
        configuration=configuration,
        task_count=len(scores),
        completion_rate=_bool_summary(scores, "completed"),
        retrieval_recall=_float_summary(scores, "retrieval_recall"),
        citation_precision=_float_summary(scores, "citation_precision"),
        unsupported_claim_rate=_float_summary(scores, "unsupported_claim_rate"),
        abstention_accuracy=_bool_summary(scores, "abstention_correct"),
        clarification_accuracy=_bool_summary(scores, "clarification_correct"),
        prompt_injection_attack_success_rate=_bool_summary(scores, "injection_attack_succeeded"),
        policy_block_accuracy=_bool_summary(scores, "policy_block_correct"),
        leaked_restricted_content_rate=_bool_summary(scores, "leaked_restricted_content"),
        mean_model_call_count=_mean(model_calls),
        mean_tool_call_count=_mean(tool_calls),
        mean_wall_clock_ms=_mean(wall_clocks),
        total_cost=sum(s.total_cost for s in scores),
        by_category=by_category,
    )
