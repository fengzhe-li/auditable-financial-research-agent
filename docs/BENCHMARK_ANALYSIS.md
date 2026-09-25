# Benchmark Analysis

**Status: an analysis layer over existing results, not a new experiment.** Everything in this document describes `afra.benchmark.analysis` / `afra.benchmark.failure_taxonomy` / `afra.benchmark.report` (added after [PHASE_6_REPORT.md](PHASE_6_REPORT.md)'s deterministic run and [PHASE_6_5_REAL_MODEL_REPORT.md](PHASE_6_5_REAL_MODEL_REPORT.md)'s live-run work): pure, read-only functions over already-computed `afra.benchmark.grader.TaskScore` lists or already-persisted `aggregate.json`/`manifest.json`/per-task result files. None of it reruns a benchmark task, calls a provider, or changes `afra.benchmark.grader`'s scoring rules, `afra.providers.real_model`, `afra.benchmark.real_model_harness`, the frozen task definitions, or Phase 4 policy enforcement. It exists because a single point-estimate rate (`{"value": 0.83, "n": 6}`) is not enough to responsibly interpret a small, expensive live run, and this project's own numbers deserve the same scrutiny it asks of the system under test.

## Denominators

**Every rate this project reports already carries its own `n`** (`afra.benchmark.aggregate.MetricSummary`) - a rate computed from 50 tasks and one computed from 2 must never be presented identically. This document's analysis layer treats denominators as a first-class citizen, not an afterthought, in three specific ways:

1. **Per-category breakdown is now complete.** Before this layer, `aggregate()`'s `by_category` reported only 5 of the 9 metrics [docs/EVALUATION_PLAN.md](EVALUATION_PLAN.md#metrics) defines - `retrieval_recall`, `citation_precision`, and `leaked_restricted_content_rate` were silently absent per-category even though every task computes them. `afra.benchmark.aggregate._BOOL_METRICS`/`_FLOAT_METRICS` now drive both the top-level aggregate and every `by_category` entry from the same list, so the two can't drift apart again.
2. **A category's `n` is not the same as how many tasks were *attempted* in that category.** `afra.benchmark.aggregate`'s `n` only ever counts tasks that reached grading. For a Phase 6.5 live run, some tasks may be infrastructure failures (see below) that never reached grading at all - `afra.benchmark.failure_taxonomy.summarize_outcomes()` and `afra.benchmark.analysis.denominator_sensitivity()`/`category_sensitivity()` make the *attempted* denominator visible alongside the *graded* one, explicitly, rather than letting a reader assume they're the same number.
3. **A metric with `n=0`** (e.g. `policy_block_accuracy` outside `sensitive_data_routing_policy`) is always reported as `{"value": null, "n": 0}`, never omitted from the report - see `test_by_category_metric_absent_for_a_category_reports_none_value_zero_n`.

## Confidence intervals

`afra.benchmark.analysis.wilson_interval(successes, n)` computes a **Wilson score interval**, not a naive normal/Wald approximation (`p̂ ± z·√(p̂(1-p̂)/n)`). This choice is deliberate and specific to this project's n: this benchmark's per-category denominators are routinely single digits (2-8 tasks), and the Wald interval is a poor approximation exactly there - it can extend below 0% or above 100%, and its actual coverage probability degrades badly for small n or for p̂ near 0 or 1 (a 2/2 or 0/2 category is common here). Wilson stays within [0, 1] by construction and has materially better small-n coverage; see Brown, Cai & DasGupta (2001), *"Interval Estimation for a Binomial Proportion,"* Statistical Science 16(2):101-133, which specifically recommends Wilson over Wald at small-to-moderate n.

**Only applied to genuine binomial-proportion metrics** (`afra.benchmark.analysis.PROPORTION_METRICS`): `completion_rate`, `abstention_accuracy`, `clarification_accuracy`, `prompt_injection_attack_success_rate`, `policy_block_accuracy`, `leaked_restricted_content_rate` - each is a count of a single Bernoulli success/failure event per task. **Deliberately not applied** to `retrieval_recall`, `citation_precision`, or `unsupported_claim_rate` - each of those is a *mean of a per-task fraction* (e.g. "2 of 3 gold documents retrieved"), not a count of one binary event, and a Wilson interval built for the latter would misrepresent the former's uncertainty. Reporting the mean and `n` alone for those three, honestly, is preferred over bolting on a statistically inappropriate interval.

Two ways to get a CI-augmented report, depending on what's available:

- `with_confidence_intervals(scores, aggregate_dict)` - exact, recomputes successes/n directly from the raw `TaskScore` list (available immediately after a run, in the same process).
- `with_confidence_intervals_from_summary(aggregate_dict)` - for analysing an already-persisted `aggregate.json` with no raw scores in hand (the common case for post-hoc analysis); recovers successes as `round(value * n)`, safe in practice since `value` was itself computed as `successes / n` and floating-point round-trip error at any realistic benchmark `n` is far below the 0.5 needed to round to the wrong integer.

### Small-n guidance

`afra.benchmark.analysis.small_n_caveat(n)` attaches a short, deterministic, **qualitative** banding alongside every CI - `n<5`: "extremely small sample, treat as anecdotal"; `n<10`: "very small sample, expect a wide interval"; `n<30`: "small sample, interpret with caution"; `n>=30`: no caveat. These thresholds are heuristic signage, not a formal statistical claim about when an interval becomes "valid" - the Wilson interval itself is well-defined at any `n>=1`; the caveat exists purely so a reader scanning a report doesn't mistake a 2-task category's `100%` for a stable finding. **A live Phase 6.5 run's frozen subset has 2 tasks per category** - every category-level CI on a live run should be read through this lens by construction, not as an exception.

## Repeat variance

`afra.benchmark.analysis.repeat_variance(per_repeat_aggregates)` reports **mean, sample standard deviation, min, max, and the raw per-repeat values** for each main metric, across a list of already-computed `aggregate.json` dicts (one per repeat - e.g. `<run_id>/<configuration>/repeat_*/aggregate.json`).

**Explicit design point, mechanically enforced**: with exactly one repeat, `stdev` is always `None`, never `0`. Sample standard deviation is undefined for `n=1`; reporting `0` would falsely claim measured perfect reproducibility that was never actually tested. `mean`/`min`/`max` still report for a single repeat (well-defined for one value), and `per_repeat_values` makes the `n=1` case visually obvious regardless of which summary numbers are shown alongside it. A repeat whose value for a given metric is `None` (e.g. a category with zero applicable tasks in that particular repeat) is excluded from that metric's mean/stdev/min/max but still recorded, as `null`, in `per_repeat_values` - never silently dropped.

## Failure taxonomy

`afra.benchmark.failure_taxonomy.classify_task_outcome()` assigns exactly one `OutcomeCategory` to a persisted task-result dict, working against the shape both `afra.benchmark.runner` (Phase 6 deterministic) and `afra.benchmark.real_model_harness` (Phase 6.5 live) already write - unmodified by this layer.

| Category | Meaning | Reached grading? |
|---|---|---|
| `incomplete` | Non-`STOP` finish_reason, not specifically `MAX_TOKENS` (e.g. `SAFETY`) | No |
| `max_tokens` | The specific, expected-common incomplete case | No |
| `rate_limited` | Every retry exhausted on a 429 | No |
| `provider_error` | Any other real-provider failure | No |
| `malformed_output` | A persisted `"score"` shape the grader's own schema doesn't recognize | Yes, but unreadable |
| `policy_blocked` | `SECURITY_BLOCKED` - correct, gradable outcome for a `block`-expected task | Yes |
| `clarification_required` | `NEEDS_CLARIFICATION` - correct, gradable outcome for an ambiguous task | Yes |
| `abstention` | `INSUFFICIENT_EVIDENCE` - correct, gradable outcome for an unanswerable task | Yes |
| `completed` | An ordinary graded completion | Yes |
| `other` | Recognized-but-uncategorizable, or a genuinely unrecognized shape | Unknown |

`afra.benchmark.failure_taxonomy.INFRASTRUCTURE_CATEGORIES` is exactly the first five rows (`is_infrastructure_failure()`) - the only ones that mean "this attempt never reached the deterministic grader at all." The next three (`policy_blocked`/`clarification_required`/`abstention`) are **not failures in any sense the taxonomy uses that word** - they are the *correct, expected* terminal state for their respective task categories, already graded via `abstention_correct`/`clarification_correct`/`policy_block_correct`. Conflating "the model was rate-limited" with "the model correctly abstained" would corrupt exactly the model-quality signal this benchmark exists to measure - keeping them in one taxonomy, but in clearly separated buckets, is what lets a report show both without confusing them.

## Infrastructure-vs-model-quality separation

**This project's harnesses already get this right structurally, before this analysis layer existed**: `afra.benchmark.real_model_harness._run_repeat()` never appends an `incomplete`/`rate_limited`/`provider_error` attempt to the list it grades - `aggregate()`'s `n` for every metric was already, by construction, the count of tasks that actually reached grading, never inflated by a provider failure counted as a wrong answer. What this layer adds is **making that separation visible in a report**, not fixing a bug in it:

- `afra.benchmark.failure_taxonomy.summarize_outcomes()` / `afra.benchmark.analysis.model_quality_report()` report `total_attempted`, `graded`, `infrastructure_failures` (broken down by category), and `graded_fraction` side by side, with an explicit note that model-quality metrics below are computed only over `graded`.
- `afra.benchmark.analysis.denominator_sensitivity()` (next section) goes further and quantifies *how much* a metric could move if infrastructure failures were instead treated as failures for that metric.

## Sensitivity / robustness analysis

`afra.benchmark.analysis.denominator_sensitivity(aggregate_dict, total_attempted)` reports each proportion metric under **two framings side by side**, never merged into one number:

- **`graded_only`** - exactly what `afra.benchmark.aggregate` already computes: the rate over only the tasks that reached grading.
- **`conservative_inclusive`** - the same numerator, re-based over `total_attempted` (every task the run was supposed to cover, including infrastructure failures): *"if every task that never reached grading is treated as a failure for this metric, how much does the rate move?"* This is a deliberately pessimistic bound, not a claim about what those tasks would actually have scored had they completed - no value is ever imputed for a task that didn't run.

`category_sensitivity()` applies the same framing per category (needs a per-category "how many were attempted" count, supplied by `afra.benchmark.report._category_totals()` from the raw task files - an `aggregate.json` alone can't supply it, since it only ever contains graded tasks). `aggregate_vs_category()` reports the delta between each category's value and the overall aggregate's, for every main metric, so a category that behaves very differently from the aggregate is visible rather than averaged away - **this is also the honest way to see "effect of denominator size on interpretation" in practice**: a category with `n=2` showing a large delta from a 50-task aggregate is a very different kind of evidence than a 40-task category showing the same delta, and the sensitivity/CI machinery above is what lets a reader tell the two apart instead of reading both deltas as equally meaningful.

## Run comparison

`afra.benchmark.analysis.compare_runs(aggregate_a, aggregate_b, ...)` is built for exactly the comparison this project will eventually need to make: Phase 6's deterministic F aggregate vs. a future Phase 6.5 live F aggregate (or repeat-to-repeat, or category-to-category). For every main metric it reports both values, both `n`'s, the delta, both Wilson intervals (when present - via `with_confidence_intervals`/`with_confidence_intervals_from_summary` applied beforehand), and whether the intervals overlap; with `by_category_a`/`by_category_b` supplied, the same comparison repeats per category.

**It deliberately never returns a replicate / partially-replicate / fail-to-replicate verdict.** That classification depends on which metrics matter for the question being asked, which is a judgement call this layer is not in a position to make automatically - see `test_compare_runs_never_returns_a_replicate_verdict`. What it returns instead is the evidence a reader needs to make that call themselves: `evidence_notes` flags metrics whose confidence intervals do not overlap (worded as *"evidence of a real difference,"* never *"fails to replicate"*) or whose point-estimate delta is large without CI data to confirm it, and every `metrics`/`by_category` entry is the raw comparison, unfiltered and unlabelled. A real, worked example already exists from Phase 6's own deterministic D vs. F comparison (`compare_runs` applied to `run_f3bb065c08c9`'s D/F aggregates): `abstention_accuracy`'s intervals do not overlap (D: 0.72, n=39; F: 1.00, n=42) - real, CI-confirmed evidence that F's abstention gate does what it's supposed to, exactly the kind of finding this function exists to surface without overclaiming on metrics where the evidence is weaker (e.g. `retrieval_recall`/`citation_precision`, which get no CI at all, by design - see above).

## How to interpret small-n live results

Putting the above together, for a Phase 6.5 live run specifically (2-task categories, an expensive and rate-limited real API):

1. **Read `graded` vs `total_attempted` first**, from `model_quality_report()` - a live run's `graded_fraction` can be well below 1.0, and every metric below it is silently scoped to the smaller number unless you also look at `denominator_sensitivity()`.
2. **Treat every category-level number as `n=2` unless proven otherwise** - `small_n_caveat()` will say so, but the instinct to read `"value": 1.0` as "the model always gets this right" needs to be actively resisted at this `n`.
3. **Prefer the aggregate's CI over any single category's** when asking "is this a real effect" - `n` is larger, the interval is narrower, and `aggregate_vs_category()` tells you whether a category's own number is really diverging or just noisy at `n=2`.
4. **Never compare a live run's numbers to Phase 6's deterministic ones without `compare_runs()`'s CI-overlap check** - a point-estimate delta alone, at this `n`, is not evidence of anything; a non-overlapping interval is real evidence, a large delta with no CI is a hypothesis, not a finding.
5. **A `rate_limited`/`provider_error` attempt is not a bad answer** - it's an attempt this run never actually got a model-quality signal from. Do not read `unsupported_claim_rate: 0.0, n=1` from a mostly-rate-limited run as "the model is perfect"; read `graded: 1, total_attempted: 14` first.

## What this layer does not claim

- No p-values, no hypothesis tests, no claim of statistical significance anywhere in this layer - a Wilson interval is a description of uncertainty given the data actually collected, not a significance test, and it is never presented as one.
- No automatic replicate/fail-to-replicate labelling (see [Run comparison](#run-comparison) above) - a human makes that call, with this layer's evidence in hand.
- No imputation - a task that never reached grading is never assigned a hypothetical score for any purpose, including the "conservative inclusive" sensitivity framing (which treats it as a failure for the metric in question, explicitly and conservatively, not as an estimate of what it "would have" scored).
- This document and the modules it describes do not change, and were built without running, any real-model live experiment - see the [ROADMAP.md](ROADMAP.md#recording-deviations) entry recorded alongside this layer for what was and wasn't touched.
