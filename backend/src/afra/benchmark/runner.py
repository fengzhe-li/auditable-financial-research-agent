"""Runs a full benchmark suite across one or more configurations and
persists everything docs/ROADMAP.md's Phase 6 entry requires: benchmark
version, configuration, run_id, task-level results, aggregate results,
failures, and enough of the trace to reproduce each score.

Persisted as plain JSON files under backend/benchmark/results/<run_id>/,
not new SQLite tables - a deliberate architectural choice (see
docs/PHASE_6_REPORT.md's methodology section): a benchmark run is a batch,
offline, versioned artifact, not part of any live ResearchTask's audit
trail, and keeping it as plain files makes a run trivially diffable,
inspectable, and re-runnable without growing the core application schema
for a fundamentally different concern. Each configuration still runs
through the real Repository/SQLite path per task (D/F) - the JSON files
are a summary/export of that, not a replacement for it; the underlying
per-task SQLite database is kept alongside the JSON as the literal
reproduction trace.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from afra.benchmark.aggregate import ConfigurationAggregate, aggregate
from afra.benchmark.baseline_provider import SingleShotBaselineProvider
from afra.benchmark.configurations import TaskRunResult, run_configuration_a, run_configuration_d, run_configuration_f
from afra.benchmark.grader import TaskScore, grade_task
from afra.benchmark.schema import BenchmarkSuite, BenchmarkTask, CONFIGURATION_D_EXCLUDED_CATEGORIES
from afra.storage.repository import Repository

CONFIGURATIONS = ("A", "D", "F")


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


@dataclass
class ConfigurationRun:
    configuration: str
    results: list[TaskRunResult]
    scores: list[TaskScore]
    aggregate: ConfigurationAggregate


@dataclass
class BenchmarkRun:
    run_id: str
    benchmark_version: str
    configurations: dict[str, ConfigurationRun]


def _tasks_for_configuration(suite: BenchmarkSuite, configuration: str) -> list[BenchmarkTask]:
    if configuration == "D":
        return [t for t in suite.tasks if t.category not in CONFIGURATION_D_EXCLUDED_CATEGORIES]
    return list(suite.tasks)


def run_benchmark(
    suite: BenchmarkSuite, results_dir: str | Path, configurations: tuple[str, ...] = CONFIGURATIONS
) -> BenchmarkRun:
    run_id = _new_run_id()
    run_dir = Path(results_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    config_runs: dict[str, ConfigurationRun] = {}
    for configuration in configurations:
        tasks = _tasks_for_configuration(suite, configuration)
        results: list[TaskRunResult] = []
        scores: list[TaskScore] = []

        config_dir = run_dir / configuration
        tasks_dir = config_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

        if configuration == "A":
            provider = SingleShotBaselineProvider()
            for task in tasks:
                result = run_configuration_a(task, provider)
                score = grade_task(task, result)
                results.append(result)
                scores.append(score)
                _write_task_result(tasks_dir, task, result, score)
        else:
            db_path = config_dir / "tasks.db"
            repository = Repository(db_path)
            run_fn = run_configuration_d if configuration == "D" else run_configuration_f
            try:
                for task in tasks:
                    result = run_fn(task, repository)
                    score = grade_task(task, result)
                    results.append(result)
                    scores.append(score)
                    _write_task_result(tasks_dir, task, result, score)
            finally:
                repository.close()

        config_aggregate = aggregate(configuration, scores)
        (config_dir / "aggregate.json").write_text(json.dumps(config_aggregate.to_dict(), indent=2) + "\n")
        config_runs[configuration] = ConfigurationRun(
            configuration=configuration, results=results, scores=scores, aggregate=config_aggregate
        )

    manifest = {
        "run_id": run_id,
        "benchmark_version": suite.version,
        "configurations": list(configurations),
        "task_count": len(suite.tasks),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    return BenchmarkRun(run_id=run_id, benchmark_version=suite.version, configurations=config_runs)


def _write_task_result(tasks_dir: Path, task: BenchmarkTask, result: TaskRunResult, score: TaskScore) -> None:
    payload = {"task": task.to_dict(), "result": result.to_dict(), "score": score.to_dict()}
    (tasks_dir / f"{task.task_id}.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")
