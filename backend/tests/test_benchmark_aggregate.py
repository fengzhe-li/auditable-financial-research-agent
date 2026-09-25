"""afra.benchmark.aggregate tests - kept separate from the Phase 6
configuration/grader/schema test files per this project's convention.

Covers the by_category completeness fix (this phase's benchmark-analysis
work): before it, `by_category` only ever reported 5 of the 9 metrics
docs/EVALUATION_PLAN.md defines (retrieval_recall, citation_precision, and
leaked_restricted_content_rate were missing per-category, even though the
top-level aggregate always had them) - see requirement 1's "per-category
breakdown" in docs/BENCHMARK_ANALYSIS.md.
"""

from __future__ import annotations

from afra.benchmark.aggregate import aggregate, applicable_values
from afra.benchmark.grader import TaskScore

_ALL_METRIC_NAMES = (
    "completion_rate",
    "retrieval_recall",
    "citation_precision",
    "unsupported_claim_rate",
    "abstention_accuracy",
    "clarification_accuracy",
    "prompt_injection_attack_success_rate",
    "policy_block_accuracy",
    "leaked_restricted_content_rate",
)


def _score(**kwargs) -> TaskScore:
    defaults = dict(task_id="t", configuration="F", category="factual_retrieval")
    defaults.update(kwargs)
    return TaskScore(**defaults)


def test_by_category_reports_every_metric_docs_evaluation_plan_defines():
    scores = [
        _score(
            task_id="fr-1",
            category="factual_retrieval",
            completed=True,
            retrieval_recall=1.0,
            citation_precision=1.0,
            unsupported_claim_rate=0.0,
        ),
        _score(
            task_id="sd-1",
            category="sensitive_data_routing_policy",
            completed=True,
            policy_block_correct=True,
            leaked_restricted_content=False,
        ),
    ]

    result = aggregate("F", scores).to_dict()

    for category in ("factual_retrieval", "sensitive_data_routing_policy"):
        entry = result["by_category"][category]
        for metric_name in _ALL_METRIC_NAMES:
            assert metric_name in entry, f"{metric_name} missing from by_category[{category}]"
            assert set(entry[metric_name]) == {"value", "n"}


def test_by_category_retrieval_recall_and_citation_precision_are_now_populated():
    """The exact gap this fix closes: these two were entirely absent from
    by_category before, even though every D/F task computes them."""
    scores = [
        _score(category="factual_retrieval", retrieval_recall=0.5, citation_precision=0.75),
        _score(category="factual_retrieval", retrieval_recall=1.0, citation_precision=1.0),
    ]

    result = aggregate("F", scores).to_dict()
    entry = result["by_category"]["factual_retrieval"]

    assert entry["retrieval_recall"] == {"value": 0.75, "n": 2}
    assert entry["citation_precision"] == {"value": 0.875, "n": 2}


def test_by_category_leaked_restricted_content_rate_is_now_populated():
    scores = [
        _score(category="sensitive_data_routing_policy", leaked_restricted_content=False),
        _score(category="sensitive_data_routing_policy", leaked_restricted_content=True),
    ]

    result = aggregate("F", scores).to_dict()
    entry = result["by_category"]["sensitive_data_routing_policy"]

    assert entry["leaked_restricted_content_rate"] == {"value": 0.5, "n": 2}


def test_by_category_metric_absent_for_a_category_reports_none_value_zero_n():
    """A category with no applicable tasks for a given metric (e.g.
    policy_block_accuracy outside sensitive_data_routing_policy) still gets
    the key, with value=None/n=0 - never silently omitted."""
    scores = [_score(category="factual_retrieval", completed=True)]

    result = aggregate("F", scores).to_dict()
    entry = result["by_category"]["factual_retrieval"]

    assert entry["policy_block_accuracy"] == {"value": None, "n": 0}


def test_top_level_aggregate_unaffected_by_the_by_category_fix():
    """Backward compatibility: the top-level (non-category) aggregate
    fields are computed by a separate, unmodified code path and must be
    identical to before."""
    scores = [
        _score(category="factual_retrieval", completed=True, retrieval_recall=1.0, citation_precision=1.0),
        _score(category="conflicting_evidence", completed=False, abstention_correct=True),
    ]

    result = aggregate("F", scores).to_dict()

    assert result["task_count"] == 2
    assert result["completion_rate"] == {"value": 0.5, "n": 2}
    assert result["retrieval_recall"] == {"value": 1.0, "n": 1}


def test_applicable_values_excludes_inapplicable_and_none_scores():
    scores = [
        _score(category="c", completed=True, applicable=True),
        _score(category="c", completed=False, applicable=True),
        _score(category="c", completed=None, applicable=True),  # None - excluded
        _score(category="c", completed=True, applicable=False),  # inapplicable - excluded
    ]

    values = applicable_values(scores, "completed")

    assert values == [True, False]
