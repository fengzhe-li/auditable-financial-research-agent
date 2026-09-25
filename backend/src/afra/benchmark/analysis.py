"""Benchmark analysis layer - see docs/BENCHMARK_ANALYSIS.md.

Deepens interpretation of Phase 6 deterministic and Phase 6.5 live run
results *without changing the underlying experiment*: every function here
is a pure, read-only view over already-computed
afra.benchmark.grader.TaskScore lists, already-written aggregate.json/
manifest.json dicts, or already-persisted per-task result dicts. Nothing
here reruns a task, drafts a claim, calls a provider, or touches
afra.providers.real_model / afra.benchmark.real_model_harness /
backend/scripts/run_real_model_benchmark.py - it only reads what those
modules already produced.

Four analytical additions on top of afra.benchmark.aggregate
(unmodified except for the by_category completeness fix - see that
module):

1. Wilson confidence intervals for proportion-style metrics
   (wilson_interval / with_confidence_intervals).
2. Repeat-to-repeat variance for multi-repeat runs (repeat_variance) -
   never invented for a single-repeat run.
3. A denominator-sensitivity view separating "graded-only" (what
   afra.benchmark.aggregate already reports) from a conservative
   "what if every infrastructure failure were scored as a failure"
   framing (denominator_sensitivity), at both aggregate and per-category
   level.
4. A structured, evidence-only run comparison (compare_runs) - it reports
   deltas and CI overlap, and explicitly does not auto-conclude
   replicate/partially-replicate/fail-to-replicate; that judgement is left
   to the reader.

See afra.benchmark.failure_taxonomy for the companion outcome
classification (which attempts are infrastructure failures vs which
categories are gradable-but-not-a-plain-completion), which
denominator_sensitivity below builds on.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from afra.benchmark.aggregate import applicable_values
from afra.benchmark.grader import TaskScore

# 95% two-sided Wilson interval - see wilson_interval()'s docstring for why
# Wilson rather than a naive normal approximation.
DEFAULT_Z = 1.959963984540054

# The proportion-style ("k successes out of n Bernoulli trials") metrics a
# confidence interval is statistically meaningful for - see
# with_confidence_intervals()'s docstring for why retrieval_recall/
# citation_precision/unsupported_claim_rate are deliberately excluded.
PROPORTION_METRICS: tuple[tuple[str, str], ...] = (
    ("completion_rate", "completed"),
    ("abstention_accuracy", "abstention_correct"),
    ("clarification_accuracy", "clarification_correct"),
    ("prompt_injection_attack_success_rate", "injection_attack_succeeded"),
    ("policy_block_accuracy", "policy_block_correct"),
    ("leaked_restricted_content_rate", "leaked_restricted_content"),
)

# The metrics repeat_variance()/denominator_sensitivity() operate over -
# afra.benchmark.aggregate.ConfigurationAggregate's own metric fields, by
# their to_dict() key.
MAIN_METRICS: tuple[str, ...] = (
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


# --------------------------------------------------------------------------
# 1. Wilson confidence intervals
# --------------------------------------------------------------------------


@dataclass
class WilsonInterval:
    """A Wilson score interval for a binomial proportion - see
    wilson_interval()'s docstring. point/n/lower/upper are all None when
    n == 0 (no interval is defined over zero observations); this is never
    silently reported as a 0.0-1.0 interval or omitted.
    """

    point: float | None
    n: int
    lower: float | None
    upper: float | None

    def to_dict(self) -> dict:
        return {"point": self.point, "n": self.n, "lower": self.lower, "upper": self.upper}


def wilson_interval(successes: int, n: int, z: float = DEFAULT_Z) -> WilsonInterval:
    """The Wilson score interval for `successes` out of `n` Bernoulli
    trials, at confidence level implied by `z` (default 1.96 ~ 95%).

    Preferred over a naive normal ("Wald") approximation
    (p_hat +/- z*sqrt(p_hat*(1-p_hat)/n)) specifically because this
    benchmark's per-category n is routinely small (single digits) - the
    Wald interval is a poor approximation there: it can extend below 0 or
    above 1, and its actual coverage probability degrades badly for small n
    or p_hat near 0 or 1 (exactly the case for e.g. a 2-task category
    scoring 2/2 or 0/2). The Wilson interval stays within [0, 1] by
    construction and has much better small-n coverage - see Brown, Cai &
    DasGupta (2001), "Interval Estimation for a Binomial Proportion",
    Statistical Science 16(2):101-133, which specifically recommends Wilson
    over Wald for small-to-moderate n.

    Returns (None, n, None, None) for n == 0 - there is no interval to
    report over zero observations, and this function never invents one.
    """
    if n == 0:
        return WilsonInterval(point=None, n=0, lower=None, upper=None)
    if successes < 0 or successes > n:
        raise ValueError(f"successes ({successes}) must be between 0 and n ({n})")

    p_hat = successes / n
    z2 = z * z
    denominator = 1 + z2 / n
    center = (p_hat + z2 / (2 * n)) / denominator
    margin = (z * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n))) / denominator
    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return WilsonInterval(point=p_hat, n=n, lower=lower, upper=upper)


def small_n_caveat(n: int) -> str | None:
    """A short, deterministic, qualitative caveat for a proportion metric's
    reliability given its denominator - heuristic banding, not a formal
    statistical claim (see docs/BENCHMARK_ANALYSIS.md#small-n-guidance for
    why these specific thresholds and what they are/are not). Returns None
    for n considered large enough that no caveat is warranted (n >= 30, the
    usual rough threshold for a normal approximation to start being
    reasonable - moot here since Wilson is used regardless, but still a
    reasonable line for "stop flagging this").
    """
    if n == 0:
        return "n=0 - no observations; no rate can be reported"
    if n < 5:
        return f"n={n} - extremely small sample; treat this rate as anecdotal, not a stable estimate"
    if n < 10:
        return f"n={n} - very small sample; expect a wide confidence interval"
    if n < 30:
        return f"n={n} - small sample; interpret with caution alongside the confidence interval"
    return None


def with_confidence_intervals(scores: list[TaskScore], metric_summaries: dict) -> dict:
    """Augments an already-computed aggregate dict (typically
    afra.benchmark.aggregate.ConfigurationAggregate.to_dict(), or one
    category's entry from its by_category) with a Wilson interval for each
    proportion-style metric present in it - completion_rate,
    abstention_accuracy, clarification_accuracy,
    prompt_injection_attack_success_rate, policy_block_accuracy,
    leaked_restricted_content_rate (PROPORTION_METRICS above).

    Deliberately does *not* add an interval for retrieval_recall,
    citation_precision, or unsupported_claim_rate: those are means of a
    per-task *fraction* (e.g. "2 of 3 required documents retrieved"), not
    counts of a single Bernoulli success/failure event - a Wilson interval
    (built for exactly the latter) would misrepresent their uncertainty.
    Reporting a mean and n for those, without a binomial-proportion CI
    bolted on, is the honest choice - see
    docs/BENCHMARK_ANALYSIS.md#confidence-intervals.

    `scores` supplies the exact successes/n for each metric (recomputed via
    afra.benchmark.aggregate.applicable_values(), the same filter
    aggregate() itself uses) - the interval is never back-derived from
    metric_summaries' float `value`, to avoid floating-point round-trip
    error changing a genuine integer success count.
    """
    augmented = dict(metric_summaries)
    for metric_name, field_name in PROPORTION_METRICS:
        summary = augmented.get(metric_name)
        if not isinstance(summary, dict):
            continue
        values = applicable_values(scores, field_name)
        successes = sum(1 for v in values if v)
        interval = wilson_interval(successes, len(values))
        augmented[metric_name] = {
            **summary,
            "confidence_interval": interval.to_dict(),
            "small_n_caveat": small_n_caveat(len(values)),
        }
    return augmented


def with_confidence_intervals_from_summary(metric_summaries: dict) -> dict:
    """Like with_confidence_intervals() above, but for analysing an
    already-persisted aggregate dict (e.g. a run's aggregate.json read from
    disk, possibly long after the run finished) with no raw TaskScore
    objects available to recompute successes/n from. Successes are
    recovered as round(value * n) instead - safe in practice, since `value`
    was itself originally computed as successes/n, and floating-point
    round-trip error at any realistic benchmark n is far smaller than the
    0.5 needed to round to the wrong integer. Prefer
    with_confidence_intervals() (exact) whenever the raw scores are still
    available (e.g. immediately after a run, in the same process); use this
    one for post-hoc analysis of historical results - which is the common
    case run comparison (compare_runs() below) needs to support.
    """
    augmented = dict(metric_summaries)
    for metric_name, _ in PROPORTION_METRICS:
        summary = augmented.get(metric_name)
        if not isinstance(summary, dict) or summary.get("value") is None or not summary.get("n"):
            continue
        n = summary["n"]
        successes = round(summary["value"] * n)
        interval = wilson_interval(successes, n)
        augmented[metric_name] = {
            **summary,
            "confidence_interval": interval.to_dict(),
            "small_n_caveat": small_n_caveat(n),
        }
    return augmented


# --------------------------------------------------------------------------
# 2. Repeat variance
# --------------------------------------------------------------------------


@dataclass
class MetricVariance:
    metric: str
    per_repeat_values: list[float | None]
    mean: float | None
    stdev: float | None
    minimum: float | None
    maximum: float | None

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "per_repeat_values": self.per_repeat_values,
            "mean": self.mean,
            "stdev": self.stdev,
            "min": self.minimum,
            "max": self.maximum,
        }


def repeat_variance(per_repeat_aggregates: list[dict], metrics: tuple[str, ...] = MAIN_METRICS) -> dict:
    """mean/stdev/min/max/per-repeat values for each of `metrics`, across a
    list of already-computed aggregate dicts (one per repeat - e.g. read
    from `<run>/<configuration>/repeat_NN/aggregate.json`).

    Requirement this exists to satisfy literally: "Do not invent variance
    for single-repeat runs." With exactly one repeat, `stdev` is always
    None (sample standard deviation is undefined for n=1, not "0" -
    reporting 0 would falsely claim perfect reproducibility that was never
    measured) - min/max/mean still report (they're well-defined for a
    single value) but per_repeat_values makes the n=1 case visually
    obvious regardless. A repeat whose value for a metric is None (e.g. a
    category with zero applicable tasks that repeat) is excluded from
    that metric's mean/stdev/min/max - the None is still visible in
    per_repeat_values, not silently dropped.
    """
    result: dict[str, dict] = {}
    for metric in metrics:
        raw_values: list[float | None] = []
        for repeat_aggregate in per_repeat_aggregates:
            summary = repeat_aggregate.get(metric)
            value = summary.get("value") if isinstance(summary, dict) else None
            raw_values.append(value)
        present = [v for v in raw_values if v is not None]
        stdev = statistics.stdev(present) if len(present) >= 2 else None
        result[metric] = MetricVariance(
            metric=metric,
            per_repeat_values=raw_values,
            mean=statistics.mean(present) if present else None,
            stdev=stdev,
            minimum=min(present) if present else None,
            maximum=max(present) if present else None,
        ).to_dict()
    return {
        "repeat_count": len(per_repeat_aggregates),
        "metrics": result,
    }


# --------------------------------------------------------------------------
# 3. Denominator sensitivity / infrastructure-vs-model-quality separation
# --------------------------------------------------------------------------


@dataclass
class ModelQualityReport:
    """Explicit separation of model-quality outcomes from infrastructure/
    provider failures for one run - see
    docs/BENCHMARK_ANALYSIS.md#infrastructure-vs-model-quality-separation.
    Every count here has an explicit denominator; nothing is inferred.
    """

    total_attempted: int
    graded: int
    infrastructure_failures: int
    infrastructure_failures_by_category: dict[str, int]
    graded_fraction: float | None

    def to_dict(self) -> dict:
        return {
            "total_attempted": self.total_attempted,
            "graded": self.graded,
            "infrastructure_failures": self.infrastructure_failures,
            "infrastructure_failures_by_category": dict(self.infrastructure_failures_by_category),
            "graded_fraction": self.graded_fraction,
            "note": (
                "model-quality metrics (completion_rate, unsupported_claim_rate, etc.) below are "
                "computed only over the 'graded' tasks - an infrastructure failure (incomplete/"
                "max_tokens/rate_limited/provider_error) is never counted as an unsupported or "
                "incorrect claim; it simply never reached grading. See "
                "docs/BENCHMARK_ANALYSIS.md#infrastructure-vs-model-quality-separation."
            ),
        }


def model_quality_report(outcome_summary: dict) -> ModelQualityReport:
    """Builds a ModelQualityReport from an
    afra.benchmark.failure_taxonomy.OutcomeSummary.to_dict() (or an
    equivalently-shaped dict) - a thin, explicit repackaging, not a new
    computation, so the separation this benchmark already makes
    structurally (afra.benchmark.real_model_harness never appends an
    infrastructure-failed attempt to the list it grades) is also visible in
    a report rather than only implicit in the code.
    """
    by_category = {
        category: count
        for category, count in outcome_summary.get("by_category", {}).items()
        if category in {"incomplete", "max_tokens", "rate_limited", "provider_error", "malformed_output"}
    }
    total = outcome_summary.get("total", 0)
    graded = outcome_summary.get("gradable", 0)
    return ModelQualityReport(
        total_attempted=total,
        graded=graded,
        infrastructure_failures=outcome_summary.get("infrastructure_failures", 0),
        infrastructure_failures_by_category=by_category,
        graded_fraction=(graded / total) if total else None,
    )


def denominator_sensitivity(aggregate_dict: dict, total_attempted: int) -> dict:
    """For each proportion-style metric (PROPORTION_METRICS), reports two
    framings side by side:

    - "graded_only": exactly what afra.benchmark.aggregate already
      computes - the rate over only the tasks that reached grading
      (denominator = the metric's own `n`).
    - "conservative_inclusive": the same numerator, but re-based over
      `total_attempted` (every task the run was supposed to cover,
      including infrastructure failures) - i.e. "if every task that never
      reached grading is treated as a failure for this metric, how much
      does the rate move?" This is a deliberately pessimistic bound, not a
      claim about what those tasks would have scored had they completed -
      see docs/BENCHMARK_ANALYSIS.md#sensitivity-analysis for why this
      framing (rather than e.g. imputing a value) is the honest one to
      show.

    total_attempted is a caller-supplied denominator (e.g. a Phase 6.5
    live run's frozen subset size) rather than inferred, since an
    aggregate dict alone only knows about tasks that reached grading -
    exactly the gap this function exists to make visible.
    """
    sensitivity: dict[str, dict] = {}
    for metric_name, _ in PROPORTION_METRICS:
        summary = aggregate_dict.get(metric_name)
        if not isinstance(summary, dict) or summary.get("value") is None:
            sensitivity[metric_name] = {"graded_only": summary, "conservative_inclusive": None}
            continue
        graded_n = summary["n"]
        successes = round(summary["value"] * graded_n)
        conservative_value = successes / total_attempted if total_attempted else None
        sensitivity[metric_name] = {
            "graded_only": {"value": summary["value"], "n": graded_n},
            "conservative_inclusive": {
                "value": conservative_value,
                "n": total_attempted,
                "assumption": "every non-graded attempt counted as a failure for this metric",
            },
            "shift": (
                (conservative_value - summary["value"]) if conservative_value is not None else None
            ),
        }
    return sensitivity


def category_sensitivity(by_category: dict, category_totals_attempted: dict[str, int]) -> dict:
    """denominator_sensitivity() applied per category - "category-level
    sensitivity" - plus the delta between each category's graded-only rate
    and the overall aggregate's, so a category that behaves very
    differently from the aggregate is visible rather than averaged away.
    `category_totals_attempted` maps category -> how many tasks in that
    category were attempted in total (graded + infrastructure failures);
    a category missing from it is skipped (sensitivity cannot be computed
    without knowing how many were attempted).
    """
    return {
        category: denominator_sensitivity(entry, category_totals_attempted[category])
        for category, entry in by_category.items()
        if category in category_totals_attempted
    }


def aggregate_vs_category(aggregate_dict: dict, by_category: dict) -> dict:
    """For each proportion/mean metric, the delta between each category's
    value and the overall aggregate's value - "comparison of aggregate vs
    per-category behaviour". A category whose value is None for a metric
    (not applicable, e.g. policy_block_accuracy outside
    sensitive_data_routing_policy) is reported with delta=None, not
    silently skipped.
    """
    comparison: dict[str, dict] = {}
    for metric in MAIN_METRICS:
        overall = aggregate_dict.get(metric, {})
        overall_value = overall.get("value") if isinstance(overall, dict) else None
        per_category = {}
        for category, entry in by_category.items():
            cat_summary = entry.get(metric)
            cat_value = cat_summary.get("value") if isinstance(cat_summary, dict) else None
            delta = (cat_value - overall_value) if (cat_value is not None and overall_value is not None) else None
            per_category[category] = {
                "value": cat_value,
                "n": cat_summary.get("n") if isinstance(cat_summary, dict) else None,
                "delta_from_aggregate": delta,
            }
        comparison[metric] = {"aggregate_value": overall_value, "by_category": per_category}
    return comparison


# --------------------------------------------------------------------------
# 4. Run comparison
# --------------------------------------------------------------------------


def _intervals_overlap(a: dict | None, b: dict | None) -> bool | None:
    if not a or not b or a.get("lower") is None or b.get("lower") is None:
        return None
    return a["lower"] <= b["upper"] and b["lower"] <= a["upper"]


@dataclass
class MetricComparison:
    metric: str
    value_a: float | None
    value_b: float | None
    n_a: int | None
    n_b: int | None
    delta: float | None
    ci_a: dict | None
    ci_b: dict | None
    ci_overlap: bool | None

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "value_a": self.value_a,
            "value_b": self.value_b,
            "n_a": self.n_a,
            "n_b": self.n_b,
            "delta": self.delta,
            "ci_a": self.ci_a,
            "ci_b": self.ci_b,
            "ci_overlap": self.ci_overlap,
        }


def compare_runs(
    aggregate_a: dict,
    aggregate_b: dict,
    *,
    label_a: str = "A",
    label_b: str = "B",
    by_category_a: dict | None = None,
    by_category_b: dict | None = None,
) -> dict:
    """A structured, evidence-only comparison between two already-computed
    aggregates (e.g. Phase 6's deterministic F aggregate.json vs a future
    Phase 6.5 live F aggregate.json, or one repeat vs another). Reports,
    per metric: both values, both n's, the delta, both Wilson intervals
    (when the aggregate dicts already carry them - see
    with_confidence_intervals(); None if not present) and whether the
    intervals overlap.

    Deliberately does **not** compute or return a replicate/
    partially-replicate/fail-to-replicate verdict - that classification
    requires human judgement about which deltas matter for this project's
    purposes (see docs/BENCHMARK_ANALYSIS.md#run-comparison), and
    auto-labelling it here would be exactly the kind of unearned
    statistical confidence this analysis layer is trying not to produce.
    What it returns is the evidence a reader needs to make that call:
    metric-by-metric deltas and CI overlap at both the aggregate and
    category level, plus a purely descriptive (never prescriptive)
    `evidence_notes` list flagging metrics with a non-overlapping CI or a
    large delta, worded as "differs" / "does not differ", never as
    "replicates" / "fails to replicate".
    """
    metric_comparisons = []
    for metric in MAIN_METRICS:
        summary_a = aggregate_a.get(metric, {}) or {}
        summary_b = aggregate_b.get(metric, {}) or {}
        value_a, value_b = summary_a.get("value"), summary_b.get("value")
        ci_a, ci_b = summary_a.get("confidence_interval"), summary_b.get("confidence_interval")
        metric_comparisons.append(
            MetricComparison(
                metric=metric,
                value_a=value_a,
                value_b=value_b,
                n_a=summary_a.get("n"),
                n_b=summary_b.get("n"),
                delta=(value_b - value_a) if (value_a is not None and value_b is not None) else None,
                ci_a=ci_a,
                ci_b=ci_b,
                ci_overlap=_intervals_overlap(ci_a, ci_b),
            ).to_dict()
        )

    evidence_notes = []
    for comparison in metric_comparisons:
        if comparison["ci_overlap"] is False:
            evidence_notes.append(
                f"{comparison['metric']}: confidence intervals do not overlap "
                f"({label_a}={comparison['value_a']!r} n={comparison['n_a']}, "
                f"{label_b}={comparison['value_b']!r} n={comparison['n_b']}) - evidence of a real difference"
            )
        elif comparison["delta"] is not None and abs(comparison["delta"]) >= 0.2:
            evidence_notes.append(
                f"{comparison['metric']}: large point-estimate delta ({comparison['delta']:+.2f}) "
                f"between {label_a} and {label_b}, though confidence intervals were not both available to confirm it"
            )

    category_comparison = None
    if by_category_a is not None and by_category_b is not None:
        shared_categories = sorted(set(by_category_a) & set(by_category_b))
        category_comparison = {
            category: {
                metric: {
                    "value_a": (by_category_a[category].get(metric) or {}).get("value"),
                    "value_b": (by_category_b[category].get(metric) or {}).get("value"),
                }
                for metric in MAIN_METRICS
            }
            for category in shared_categories
        }

    return {
        "label_a": label_a,
        "label_b": label_b,
        "metrics": metric_comparisons,
        "evidence_notes": evidence_notes,
        "by_category": category_comparison,
        "interpretation_note": (
            "This comparison reports evidence (deltas, confidence-interval overlap) only. It does not "
            "conclude 'replicate' / 'partially replicate' / 'fail to replicate' - that judgement depends "
            "on which metrics matter for the question being asked and is left to the reader. See "
            "docs/BENCHMARK_ANALYSIS.md#run-comparison."
        ),
    }
