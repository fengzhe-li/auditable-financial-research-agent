"""File-reading glue between afra.benchmark.analysis/failure_taxonomy
(pure functions) and on-disk run artifacts - see docs/BENCHMARK_ANALYSIS.md.

Deliberately the only module in this layer that touches the filesystem;
afra.benchmark.analysis and afra.benchmark.failure_taxonomy stay pure and
directly unit-testable against hand-built dicts. This module only *reads*
existing files - afra.benchmark.runner (Phase 6) and
afra.benchmark.real_model_harness (Phase 6.5, untouched by this layer)
remain the only code that writes them.

Works against both directory shapes this project's two harnesses already
produce:
- Phase 6 deterministic: <results_dir>/<run_id>/<configuration>/tasks/*.json
  and .../aggregate.json
- Phase 6.5 live: <results_dir>/<run_id>/<configuration>/repeat_NN/tasks/*.json
  and .../repeat_NN/aggregate.json, plus a run-level manifest.json with
  incomplete_attempts/rate_limited_attempts/provider_error_attempts
"""

from __future__ import annotations

import json
from pathlib import Path

from afra.benchmark.analysis import (
    aggregate_vs_category,
    category_sensitivity,
    denominator_sensitivity,
    repeat_variance,
    with_confidence_intervals_from_summary,
)
from afra.benchmark.failure_taxonomy import OutcomeCategory, classify_task_outcome, summarize_outcomes


def load_task_jsons(tasks_dir: str | Path) -> list[dict]:
    """Every *.json file directly under `tasks_dir`, parsed - the shared
    shape afra.benchmark.runner and afra.benchmark.real_model_harness both
    write one file per task into. Returns [] if the directory doesn't
    exist (e.g. a configuration that was never run), rather than raising -
    this is a read-only reporting layer over whatever actually exists on
    disk.
    """
    directory = Path(tasks_dir)
    if not directory.is_dir():
        return []
    task_jsons = []
    for path in sorted(directory.glob("*.json")):
        task_jsons.append(json.loads(path.read_text()))
    return task_jsons


def _category_totals(task_jsons: list[dict]) -> dict[str, int]:
    """How many task attempts (graded or not) exist per category - the
    denominator afra.benchmark.analysis.category_sensitivity() needs and
    an aggregate.json's by_category alone can't supply, since that dict
    only ever contains tasks that reached grading.
    """
    totals: dict[str, int] = {}
    for task_json in task_jsons:
        category = (task_json.get("task") or {}).get("category")
        if category:
            totals[category] = totals.get(category, 0) + 1
    return totals


def build_configuration_report(config_dir: str | Path) -> dict:
    """The full analysis-layer report for one configuration directory
    (Phase 6: <run_id>/<configuration>/; Phase 6.5, single repeat:
    <run_id>/<configuration>/repeat_NN/) - outcome taxonomy, a CI-augmented
    aggregate, denominator sensitivity (aggregate and per-category), and
    aggregate-vs-category comparison. Reads aggregate.json and every
    tasks/*.json file already persisted there; writes nothing.
    """
    config_dir = Path(config_dir)
    task_jsons = load_task_jsons(config_dir / "tasks")
    outcome_summary = summarize_outcomes(task_jsons).to_dict()

    aggregate_path = config_dir / "aggregate.json"
    aggregate_dict = json.loads(aggregate_path.read_text()) if aggregate_path.exists() else {}

    aggregate_with_ci = with_confidence_intervals_from_summary(aggregate_dict)
    by_category = aggregate_dict.get("by_category", {})
    by_category_with_ci = {
        category: with_confidence_intervals_from_summary(entry) for category, entry in by_category.items()
    }

    category_totals = _category_totals(task_jsons)
    total_attempted = len(task_jsons)

    return {
        "configuration": aggregate_dict.get("configuration"),
        "outcome_summary": outcome_summary,
        "aggregate": aggregate_with_ci,
        "by_category": by_category_with_ci,
        "denominator_sensitivity": {
            "aggregate": denominator_sensitivity(aggregate_dict, total_attempted),
            "by_category": category_sensitivity(by_category, category_totals),
        },
        "aggregate_vs_category": aggregate_vs_category(aggregate_dict, by_category),
    }


def build_repeat_variance_report(repeat_dirs: list[str | Path]) -> dict:
    """afra.benchmark.analysis.repeat_variance() over a list of already-run
    repeat directories (each containing its own aggregate.json) - e.g.
    every `<run_id>/<configuration>/repeat_*/` directory for one
    configuration of a Phase 6.5 live run. A single-element list correctly
    produces stdev=None for every metric (see repeat_variance()'s
    docstring) rather than inventing a variance from one observation.
    """
    aggregates = []
    for repeat_dir in repeat_dirs:
        path = Path(repeat_dir) / "aggregate.json"
        if path.exists():
            aggregates.append(json.loads(path.read_text()))
    return repeat_variance(aggregates)


def outcome_category_for_task_file(task_json_path: str | Path) -> OutcomeCategory:
    """Convenience one-file wrapper around
    afra.benchmark.failure_taxonomy.classify_task_outcome(), for callers
    inspecting a single persisted task result rather than a whole
    directory."""
    return classify_task_outcome(json.loads(Path(task_json_path).read_text()))
