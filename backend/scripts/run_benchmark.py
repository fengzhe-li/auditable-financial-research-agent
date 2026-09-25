"""Runs the full Phase 6 benchmark (all three configurations, A/D/F)
against backend/benchmark/tasks_v1.json, and prints the per-configuration
aggregate table plus a handful of representative examples. Results are
persisted under backend/benchmark/results/<run_id>/ - see
afra.benchmark.runner's module docstring for the persisted layout.

Not part of the pytest suite (docs/ROADMAP.md's Phase 6 entry requires
benchmark tests to be added *separately* from core correctness tests -
tests/test_benchmark_*.py cover the harness/grader/schema directly; this
script is what actually produces the numbers reported in
docs/PHASE_6_REPORT.md).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from afra.benchmark.runner import run_benchmark
from afra.benchmark.schema import load_benchmark


def main() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    suite = load_benchmark(backend_dir / "benchmark" / "tasks_v1.json")
    results_dir = backend_dir / "benchmark" / "results"

    print(f"Benchmark version: {suite.version}  |  tasks: {len(suite.tasks)}")
    run = run_benchmark(suite, results_dir)
    print(f"run_id: {run.run_id}\n")

    header = (
        f"{'config':7} {'n':>4} {'completion':>11} {'recall':>7} {'citation':>9} "
        f"{'unsupp.':>8} {'abstain':>8} {'clarify':>8} {'inject%':>8} {'policy':>7} "
        f"{'calls':>6} {'tools':>6} {'ms':>7}"
    )
    print(header)
    print("-" * len(header))
    for configuration in ("A", "D", "F"):
        agg = run.configurations[configuration].aggregate

        def fmt(summary) -> str:
            return f"{summary.value:.2f}" if summary.value is not None else "N/A"

        print(
            f"{configuration:7} {agg.task_count:4d} "
            f"{fmt(agg.completion_rate):>11} {fmt(agg.retrieval_recall):>7} {fmt(agg.citation_precision):>9} "
            f"{fmt(agg.unsupported_claim_rate):>8} {fmt(agg.abstention_accuracy):>8} {fmt(agg.clarification_accuracy):>8} "
            f"{fmt(agg.prompt_injection_attack_success_rate):>8} {fmt(agg.policy_block_accuracy):>7} "
            f"{agg.mean_model_call_count or 0:6.1f} {agg.mean_tool_call_count or 0:6.1f} {agg.mean_wall_clock_ms or 0:7.2f}"
        )

    print("\nPer-category breakdown (F):")
    print(json.dumps(run.configurations["F"].aggregate.by_category, indent=2))

    print(f"\nFull results: {results_dir / run.run_id}")


if __name__ == "__main__":
    main()
