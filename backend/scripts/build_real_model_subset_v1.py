"""Generates backend/benchmark/real_model_subset_v1.json - the frozen,
14-task representative subset covering all 7 categories
afra.benchmark.real_model_harness.REQUIRED_CATEGORIES requires (every
category docs/EVALUATION_PLAN.md's metrics are defined over, except
temporal_change_analysis - see that constant's docstring for why it's
excluded). 2 tasks per category, selected by the deterministic,
documented rule in afra.benchmark.real_model_harness (TASKS_PER_CATEGORY /
_CATEGORY_OVERRIDES) - not hand-picked ad hoc. Re-running this script must
be byte-identical to the checked-in file - tests/test_real_model_harness.py
enforces this.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from afra.benchmark.real_model_harness import (
    REQUIRED_CATEGORIES,
    SUBSET_PATH,
    TASKS_PER_CATEGORY,
    RealModelSubset,
    derive_subset_task_ids,
    save_subset,
)
from afra.benchmark.schema import load_benchmark


def main() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    suite = load_benchmark(backend_dir / "benchmark" / "tasks_v1.json")
    task_ids = derive_subset_task_ids(suite)

    subset = RealModelSubset(
        version="v1",
        source_benchmark_version=suite.version,
        categories=sorted(REQUIRED_CATEGORIES),
        task_ids=task_ids,
        rationale=(
            f"Stratified: {TASKS_PER_CATEGORY} tasks from each of the 7 categories a "
            "real-model pass needs to compute every docs/EVALUATION_PLAN.md metric "
            "(unsupported-claim rate, citation precision, abstention accuracy, "
            "clarification accuracy, prompt-injection success rate, policy-block "
            "accuracy, completion rate) - factual_retrieval, multi_document_comparison, "
            "ambiguous_clarification, insufficient_evidence, conflicting_evidence, "
            "prompt_injection, sensitive_data_routing_policy. Selection is the first 2 "
            "tasks per category by task_id, except three categories with an explicit, "
            "documented override (afra.benchmark.real_model_harness._CATEGORY_OVERRIDES) "
            "so a gold-field dimension the category's tasks vary on (clarification "
            "reason / which document is missing / policy outcome) is represented rather "
            "than collapsed to one value. temporal_change_analysis is excluded - "
            "docs/PHASE_6_REPORT.md's §6 already established its retrieval_recall is a "
            "mechanical 0.5 regardless of provider (a router limitation, not something "
            "claim-drafting text can affect), so it would not produce new information."
        ),
    )
    save_subset(subset, SUBSET_PATH)
    print(f"Wrote {SUBSET_PATH} ({len(task_ids)} tasks across {len(subset.categories)} categories)")


if __name__ == "__main__":
    main()
