"""Tests for the benchmark-analysis layer (afra.benchmark.analysis /
afra.benchmark.failure_taxonomy / afra.benchmark.report) - see
docs/BENCHMARK_ANALYSIS.md.

Every test here works against hand-built dicts or a tmp_path directory
tree, never a real benchmark run and never a real model call - this layer
is a pure/read-only view over already-computed results, and its tests stay
that way too.
"""

from __future__ import annotations

import json

import pytest

from afra.benchmark.aggregate import aggregate
from afra.benchmark.analysis import (
    aggregate_vs_category,
    category_sensitivity,
    compare_runs,
    denominator_sensitivity,
    model_quality_report,
    repeat_variance,
    small_n_caveat,
    wilson_interval,
    with_confidence_intervals,
    with_confidence_intervals_from_summary,
)
from afra.benchmark.failure_taxonomy import (
    INFRASTRUCTURE_CATEGORIES,
    OutcomeCategory,
    classify_task_outcome,
    is_infrastructure_failure,
    summarize_outcomes,
)
from afra.benchmark.grader import TaskScore
from afra.benchmark.report import build_configuration_report, build_repeat_variance_report, load_task_jsons


def _score(**kwargs) -> TaskScore:
    defaults = dict(task_id="t", configuration="F", category="factual_retrieval")
    defaults.update(kwargs)
    return TaskScore(**defaults)


# -- 1. Wilson interval calculations ------------------------------------------


def test_wilson_interval_n_zero_reports_no_interval():
    interval = wilson_interval(0, 0)
    assert interval.to_dict() == {"point": None, "n": 0, "lower": None, "upper": None}


def test_wilson_interval_matches_known_reference_value():
    """5/10 at 95% (z=1.96) is a standard textbook example - Wilson centre
    ~0.5, interval roughly [0.237, 0.763]."""
    interval = wilson_interval(5, 10)
    assert interval.point == 0.5
    assert interval.n == 10
    assert interval.lower == pytest.approx(0.2366, abs=1e-3)
    assert interval.upper == pytest.approx(0.7634, abs=1e-3)


def test_wilson_interval_stays_within_zero_one_at_the_extremes():
    """10/10 - a naive Wald interval would go above 1.0; Wilson must not."""
    interval = wilson_interval(10, 10)
    assert interval.point == 1.0
    assert 0.0 <= interval.lower <= interval.upper <= 1.0
    assert interval.upper == pytest.approx(1.0)

    interval_zero = wilson_interval(0, 10)
    assert interval_zero.point == 0.0
    assert interval_zero.lower == 0.0


def test_wilson_interval_narrows_as_n_grows_for_the_same_rate():
    small = wilson_interval(5, 10)
    large = wilson_interval(500, 1000)
    assert (large.upper - large.lower) < (small.upper - small.lower)


def test_wilson_interval_rejects_impossible_successes():
    with pytest.raises(ValueError):
        wilson_interval(11, 10)
    with pytest.raises(ValueError):
        wilson_interval(-1, 10)


def test_small_n_caveat_bands():
    assert small_n_caveat(0) is not None and "n=0" in small_n_caveat(0)
    assert "extremely small" in small_n_caveat(3)
    assert "very small" in small_n_caveat(7)
    assert "small sample" in small_n_caveat(15)
    assert small_n_caveat(30) is None
    assert small_n_caveat(1000) is None


def test_with_confidence_intervals_adds_ci_only_to_proportion_metrics():
    scores = [
        _score(completed=True, retrieval_recall=1.0),
        _score(completed=True, retrieval_recall=0.5),
        _score(completed=False, retrieval_recall=0.0),
    ]
    agg = aggregate("F", scores).to_dict()

    augmented = with_confidence_intervals(scores, agg)

    assert "confidence_interval" in augmented["completion_rate"]
    ci = augmented["completion_rate"]["confidence_interval"]
    assert ci["n"] == 3
    assert ci["point"] == pytest.approx(2 / 3)
    # retrieval_recall is a mean-of-fractions metric, not a binomial
    # proportion - deliberately no CI attached.
    assert "confidence_interval" not in augmented["retrieval_recall"]


def test_with_confidence_intervals_from_summary_matches_the_scores_based_version():
    scores = [_score(completed=True) for _ in range(7)] + [_score(completed=False) for _ in range(3)]
    agg = aggregate("F", scores).to_dict()

    from_scores = with_confidence_intervals(scores, agg)
    from_summary = with_confidence_intervals_from_summary(agg)

    assert from_scores["completion_rate"]["confidence_interval"] == from_summary["completion_rate"]["confidence_interval"]


def test_with_confidence_intervals_from_summary_skips_metrics_with_no_value():
    augmented = with_confidence_intervals_from_summary({"completion_rate": {"value": None, "n": 0}})
    assert "confidence_interval" not in augmented["completion_rate"]


# -- 2. repeat aggregation / variance ------------------------------------------


def test_repeat_variance_single_repeat_never_invents_a_stdev():
    agg = {"completion_rate": {"value": 0.8, "n": 10}}
    result = repeat_variance([agg], metrics=("completion_rate",))
    metric = result["metrics"]["completion_rate"]
    assert result["repeat_count"] == 1
    assert metric["per_repeat_values"] == [0.8]
    assert metric["stdev"] is None
    assert metric["mean"] == 0.8
    assert metric["min"] == metric["max"] == 0.8


def test_repeat_variance_multiple_repeats_computes_real_stdev():
    aggs = [
        {"completion_rate": {"value": 0.8, "n": 10}},
        {"completion_rate": {"value": 1.0, "n": 10}},
        {"completion_rate": {"value": 0.6, "n": 10}},
    ]
    result = repeat_variance(aggs, metrics=("completion_rate",))
    metric = result["metrics"]["completion_rate"]
    assert metric["per_repeat_values"] == [0.8, 1.0, 0.6]
    assert metric["mean"] == pytest.approx(0.8)
    assert metric["stdev"] == pytest.approx(0.2)
    assert metric["min"] == 0.6
    assert metric["max"] == 1.0


def test_repeat_variance_handles_a_metric_missing_in_some_repeats():
    """A repeat with zero applicable tasks for a metric reports value=None
    there - repeat_variance must not crash and must exclude only that
    repeat's None from mean/stdev/min/max, while still recording it in
    per_repeat_values."""
    aggs = [
        {"completion_rate": {"value": 1.0, "n": 5}},
        {"completion_rate": {"value": None, "n": 0}},
        {"completion_rate": {"value": 0.5, "n": 5}},
    ]
    result = repeat_variance(aggs, metrics=("completion_rate",))
    metric = result["metrics"]["completion_rate"]
    assert metric["per_repeat_values"] == [1.0, None, 0.5]
    assert metric["mean"] == pytest.approx(0.75)


def test_repeat_variance_over_real_aggregate_objects():
    scores_a = [_score(completed=True), _score(completed=True)]
    scores_b = [_score(completed=True), _score(completed=False)]
    agg_a = aggregate("F", scores_a).to_dict()
    agg_b = aggregate("F", scores_b).to_dict()

    result = repeat_variance([agg_a, agg_b], metrics=("completion_rate",))
    assert result["metrics"]["completion_rate"]["per_repeat_values"] == [1.0, 0.5]


# -- Missing / null metrics -----------------------------------------------------


def test_denominator_sensitivity_handles_missing_metric_gracefully():
    result = denominator_sensitivity({}, total_attempted=10)
    assert result["completion_rate"] == {"graded_only": None, "conservative_inclusive": None}


def test_aggregate_vs_category_handles_a_category_missing_a_metric():
    aggregate_dict = {"completion_rate": {"value": 0.8, "n": 10}}
    by_category = {"factual_retrieval": {"completion_rate": {"value": None, "n": 0}}}

    result = aggregate_vs_category(aggregate_dict, by_category)

    entry = result["completion_rate"]["by_category"]["factual_retrieval"]
    assert entry == {"value": None, "n": 0, "delta_from_aggregate": None}


def test_model_quality_report_handles_zero_total_attempted():
    report = model_quality_report({"total": 0, "gradable": 0, "infrastructure_failures": 0, "by_category": {}})
    assert report.graded_fraction is None
    assert report.to_dict()["total_attempted"] == 0


# -- 3/5. infrastructure failures excluded from model-quality denominators ----


def test_denominator_sensitivity_shows_the_gap_between_graded_only_and_inclusive():
    # 8 of 10 tasks succeeded (graded); 2 never reached grading at all.
    aggregate_dict = {"completion_rate": {"value": 1.0, "n": 8}}

    result = denominator_sensitivity(aggregate_dict, total_attempted=10)

    entry = result["completion_rate"]
    assert entry["graded_only"] == {"value": 1.0, "n": 8}
    assert entry["conservative_inclusive"]["value"] == pytest.approx(0.8)
    assert entry["conservative_inclusive"]["n"] == 10
    assert entry["shift"] == pytest.approx(-0.2)


def test_denominator_sensitivity_zero_shift_when_nothing_was_excluded():
    aggregate_dict = {"completion_rate": {"value": 0.9, "n": 10}}
    result = denominator_sensitivity(aggregate_dict, total_attempted=10)
    assert result["completion_rate"]["shift"] == pytest.approx(0.0)


def test_model_quality_report_separates_graded_from_infrastructure_failures():
    outcome_summary = summarize_outcomes(
        [
            {"status": "rate_limited"},
            {"status": "provider_error"},
            {"status": "incomplete", "finish_reason": "MAX_TOKENS"},
            {"score": {"completed": True}, "result": {"final_state": "PUBLISHED"}},
        ]
    ).to_dict()

    report = model_quality_report(outcome_summary)

    assert report.total_attempted == 4
    assert report.graded == 1
    assert report.infrastructure_failures == 3
    assert report.infrastructure_failures_by_category == {
        "rate_limited": 1,
        "provider_error": 1,
        "max_tokens": 1,
    }
    assert report.graded_fraction == pytest.approx(0.25)
    note = report.to_dict()["note"]
    assert "never counted as an unsupported or incorrect claim" in note


# -- Failure taxonomy -----------------------------------------------------------


def test_classify_task_outcome_rate_limited():
    assert classify_task_outcome({"status": "rate_limited"}) == OutcomeCategory.RATE_LIMITED


def test_classify_task_outcome_provider_error():
    assert classify_task_outcome({"status": "provider_error"}) == OutcomeCategory.PROVIDER_ERROR


def test_classify_task_outcome_incomplete_max_tokens():
    assert (
        classify_task_outcome({"status": "incomplete", "finish_reason": "MAX_TOKENS"})
        == OutcomeCategory.MAX_TOKENS
    )


def test_classify_task_outcome_incomplete_non_max_tokens():
    assert (
        classify_task_outcome({"status": "incomplete", "finish_reason": "SAFETY"})
        == OutcomeCategory.INCOMPLETE
    )


def test_classify_task_outcome_policy_blocked():
    payload = {"score": {"completed": True}, "result": {"final_state": "SECURITY_BLOCKED"}}
    assert classify_task_outcome(payload) == OutcomeCategory.POLICY_BLOCKED


def test_classify_task_outcome_clarification_required():
    payload = {"score": {"completed": True}, "result": {"final_state": "NEEDS_CLARIFICATION"}}
    assert classify_task_outcome(payload) == OutcomeCategory.CLARIFICATION_REQUIRED


def test_classify_task_outcome_abstention():
    payload = {"score": {"completed": True}, "result": {"final_state": "INSUFFICIENT_EVIDENCE"}}
    assert classify_task_outcome(payload) == OutcomeCategory.ABSTENTION


def test_classify_task_outcome_completed():
    payload = {"score": {"completed": True}, "result": {"final_state": "PUBLISHED"}}
    assert classify_task_outcome(payload) == OutcomeCategory.COMPLETED


def test_classify_task_outcome_malformed_output():
    payload = {"score": {"nonsense": True}, "result": {"final_state": "PUBLISHED"}}
    assert classify_task_outcome(payload) == OutcomeCategory.MALFORMED_OUTPUT


def test_classify_task_outcome_unrecognized_shape_is_other_not_a_crash():
    assert classify_task_outcome({}) == OutcomeCategory.OTHER
    assert classify_task_outcome({"nonsense": 1}) == OutcomeCategory.OTHER
    assert classify_task_outcome(None) == OutcomeCategory.OTHER  # type: ignore[arg-type]


def test_infrastructure_categories_are_exactly_the_non_gradable_ones():
    assert INFRASTRUCTURE_CATEGORIES == {
        OutcomeCategory.INCOMPLETE,
        OutcomeCategory.MAX_TOKENS,
        OutcomeCategory.RATE_LIMITED,
        OutcomeCategory.PROVIDER_ERROR,
        OutcomeCategory.MALFORMED_OUTPUT,
    }
    assert is_infrastructure_failure(OutcomeCategory.RATE_LIMITED) is True
    assert is_infrastructure_failure(OutcomeCategory.COMPLETED) is False
    assert is_infrastructure_failure(OutcomeCategory.POLICY_BLOCKED) is False  # gradable, not a failure


def test_summarize_outcomes_reports_explicit_denominators():
    summary = summarize_outcomes(
        [
            {"status": "rate_limited"},
            {"score": {"completed": True}, "result": {"final_state": "PUBLISHED"}},
            {"score": {"completed": True}, "result": {"final_state": "PUBLISHED"}},
        ]
    )
    assert summary.total == 3
    assert summary.gradable == 2
    assert summary.infrastructure_failures == 1
    assert summary.to_dict()["infrastructure_failure_rate"] == pytest.approx(1 / 3)


def test_summarize_outcomes_empty_list():
    summary = summarize_outcomes([])
    assert summary.to_dict() == {
        "total": 0,
        "by_category": {},
        "infrastructure_failures": 0,
        "gradable": 0,
        "infrastructure_failure_rate": None,
    }


# -- Per-category aggregation (via aggregate_vs_category) ----------------------


def test_aggregate_vs_category_reports_delta_for_a_diverging_category():
    aggregate_dict = {"completion_rate": {"value": 0.8, "n": 20}}
    by_category = {
        "factual_retrieval": {"completion_rate": {"value": 1.0, "n": 10}},
        "conflicting_evidence": {"completion_rate": {"value": 0.6, "n": 10}},
    }

    result = aggregate_vs_category(aggregate_dict, by_category)

    entry = result["completion_rate"]
    assert entry["aggregate_value"] == 0.8
    assert entry["by_category"]["factual_retrieval"]["delta_from_aggregate"] == pytest.approx(0.2)
    assert entry["by_category"]["conflicting_evidence"]["delta_from_aggregate"] == pytest.approx(-0.2)


def test_category_sensitivity_only_computed_for_categories_with_a_known_total():
    by_category = {
        "factual_retrieval": {"completion_rate": {"value": 1.0, "n": 8}},
        "conflicting_evidence": {"completion_rate": {"value": 1.0, "n": 2}},
    }
    result = category_sensitivity(by_category, {"factual_retrieval": 10})

    assert "factual_retrieval" in result
    assert "conflicting_evidence" not in result  # no known total_attempted supplied
    assert result["factual_retrieval"]["completion_rate"]["conservative_inclusive"]["n"] == 10


# -- Run comparison ---------------------------------------------------------------


def test_compare_runs_flags_non_overlapping_confidence_intervals():
    agg_a = {"completion_rate": {"value": 0.5, "n": 20, "confidence_interval": wilson_interval(10, 20).to_dict()}}
    agg_b = {"completion_rate": {"value": 1.0, "n": 20, "confidence_interval": wilson_interval(20, 20).to_dict()}}

    result = compare_runs(agg_a, agg_b, label_a="D", label_b="F")

    metric = next(m for m in result["metrics"] if m["metric"] == "completion_rate")
    assert metric["delta"] == pytest.approx(0.5)
    assert metric["ci_overlap"] is False
    assert any("completion_rate" in note for note in result["evidence_notes"])


def test_compare_runs_overlapping_intervals_are_not_flagged():
    agg_a = {"completion_rate": {"value": 0.5, "n": 20, "confidence_interval": wilson_interval(10, 20).to_dict()}}
    agg_b = {"completion_rate": {"value": 0.55, "n": 20, "confidence_interval": wilson_interval(11, 20).to_dict()}}

    result = compare_runs(agg_a, agg_b)

    metric = next(m for m in result["metrics"] if m["metric"] == "completion_rate")
    assert metric["ci_overlap"] is True
    assert result["evidence_notes"] == []


def test_compare_runs_never_returns_a_replicate_verdict():
    """Requirement: "Do not claim equivalence or replication automatically" -
    the result must contain evidence, never a verdict field."""
    result = compare_runs({}, {})
    assert "replicate" not in result
    assert "verdict" not in result
    assert "classification" not in result
    assert "does not conclude" in result["interpretation_note"]


def test_compare_runs_includes_category_level_comparison_when_supplied():
    agg_a, agg_b = {"completion_rate": {"value": 0.5, "n": 10}}, {"completion_rate": {"value": 0.9, "n": 10}}
    by_cat_a = {"factual_retrieval": {"completion_rate": {"value": 0.5, "n": 10}}}
    by_cat_b = {"factual_retrieval": {"completion_rate": {"value": 0.9, "n": 10}}}

    result = compare_runs(agg_a, agg_b, by_category_a=by_cat_a, by_category_b=by_cat_b)

    assert result["by_category"]["factual_retrieval"]["completion_rate"] == {"value_a": 0.5, "value_b": 0.9}


def test_compare_runs_without_category_data_reports_none():
    result = compare_runs({}, {})
    assert result["by_category"] is None


# -- report.py: single-repeat edge cases + real directory tree -----------------


def test_load_task_jsons_missing_directory_returns_empty_list(tmp_path):
    assert load_task_jsons(tmp_path / "does_not_exist") == []


def test_build_repeat_variance_report_with_a_single_repeat_directory(tmp_path):
    repeat_dir = tmp_path / "repeat_00"
    repeat_dir.mkdir()
    (repeat_dir / "aggregate.json").write_text(json.dumps({"completion_rate": {"value": 0.75, "n": 4}}))

    result = build_repeat_variance_report([repeat_dir])

    assert result["repeat_count"] == 1
    assert result["metrics"]["completion_rate"]["stdev"] is None
    assert result["metrics"]["completion_rate"]["per_repeat_values"] == [0.75]


def test_build_repeat_variance_report_skips_a_missing_repeat_aggregate(tmp_path):
    present = tmp_path / "repeat_00"
    present.mkdir()
    (present / "aggregate.json").write_text(json.dumps({"completion_rate": {"value": 1.0, "n": 2}}))
    missing = tmp_path / "repeat_01"  # never created - simulates a crashed repeat

    result = build_repeat_variance_report([present, missing])

    assert result["repeat_count"] == 1  # the missing one was skipped, not silently counted


def test_build_configuration_report_end_to_end_over_a_fake_run_directory(tmp_path):
    """Exercises the full read -> classify -> aggregate -> CI -> sensitivity
    pipeline against a hand-built directory tree shaped exactly like a real
    afra.benchmark.runner/afra.benchmark.real_model_harness output -
    without ever running the benchmark or a real model."""
    config_dir = tmp_path / "F"
    tasks_dir = config_dir / "tasks"
    tasks_dir.mkdir(parents=True)

    (tasks_dir / "FR-01.json").write_text(
        json.dumps({"task": {"category": "factual_retrieval"}, "score": {"completed": True}, "result": {"final_state": "PUBLISHED"}})
    )
    (tasks_dir / "FR-02.json").write_text(json.dumps({"task": {"category": "factual_retrieval"}, "status": "rate_limited"}))
    (config_dir / "aggregate.json").write_text(
        json.dumps(
            {
                "configuration": "F",
                "completion_rate": {"value": 1.0, "n": 1},
                "by_category": {"factual_retrieval": {"completion_rate": {"value": 1.0, "n": 1}}},
            }
        )
    )

    report = build_configuration_report(config_dir)

    assert report["configuration"] == "F"
    assert report["outcome_summary"]["total"] == 2
    assert report["outcome_summary"]["gradable"] == 1
    assert report["outcome_summary"]["infrastructure_failures"] == 1
    assert report["aggregate"]["completion_rate"]["confidence_interval"]["n"] == 1
    # 1 graded out of 2 attempted for this category - sensitivity should
    # reflect that gap.
    fr_sensitivity = report["denominator_sensitivity"]["by_category"]["factual_retrieval"]["completion_rate"]
    assert fr_sensitivity["conservative_inclusive"]["n"] == 2
    assert fr_sensitivity["conservative_inclusive"]["value"] == pytest.approx(0.5)
