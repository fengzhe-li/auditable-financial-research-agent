"""Integration-level tests of the three benchmark harness functions
(afra.benchmark.configurations.run_configuration_a/d/f) and the full
runner, exercised for real against the frozen tasks_v1.json - not a
representative subset, the whole thing, since a deterministic harness
against fixture-only providers is fast enough (well under a second for all
150 task-runs) that there's no reason to under-test it. Kept in a separate
file from the direct grader/schema unit tests, per docs/ROADMAP.md's Phase
6 instruction to add benchmark tests separately from core correctness
tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from afra.benchmark.aggregate import aggregate
from afra.benchmark.configurations import run_configuration_a, run_configuration_d, run_configuration_f
from afra.benchmark.grader import grade_task
from afra.benchmark.runner import run_benchmark
from afra.benchmark.schema import CONFIGURATION_D_EXCLUDED_CATEGORIES, load_benchmark
from afra.storage.repository import Repository

BENCHMARK_PATH = Path(__file__).resolve().parents[1] / "benchmark" / "tasks_v1.json"


@pytest.fixture(scope="module")
def suite():
    return load_benchmark(BENCHMARK_PATH)


def test_configuration_a_runs_every_task_without_raising(suite):
    for task in suite.tasks:
        result = run_configuration_a(task)
        assert result.error is None
        assert result.answer_text is not None


def _error_is_expected(task, result) -> bool:
    """A non-None TaskRunResult.error is the correct, intended outcome for
    a task whose gold expected_policy_outcome is "block" - the harness
    catches SecurityPolicyError there deliberately (see
    afra.benchmark.configurations.run_configuration_d/f), it is not an
    unexpected crash.
    """
    return result.error is None or task.expected_policy_outcome == "block"


def test_configuration_d_and_f_run_every_included_task_without_raising(suite, db_path):
    repo_d = Repository(db_path.parent / "d.db")
    repo_f = Repository(db_path.parent / "f.db")
    try:
        for task in suite.tasks:
            if task.category not in CONFIGURATION_D_EXCLUDED_CATEGORIES:
                result_d = run_configuration_d(task, repo_d)
                assert _error_is_expected(task, result_d), f"{task.task_id}: {result_d.error}"
            result_f = run_configuration_f(task, repo_f)
            assert _error_is_expected(task, result_f), f"{task.task_id}: {result_f.error}"
    finally:
        repo_d.close()
        repo_f.close()


def test_full_benchmark_run_smoke(suite, tmp_path):
    """The full A/D/F x 50-task run, through the real runner/persistence
    path, proving the harness is genuinely reproducible end to end - not
    just that individual functions don't raise.
    """
    run = run_benchmark(suite, tmp_path / "results")

    assert len(run.configurations["A"].results) == 50
    assert len(run.configurations["D"].results) == 50 - sum(
        1 for t in suite.tasks if t.category in CONFIGURATION_D_EXCLUDED_CATEGORIES
    )
    assert len(run.configurations["F"].results) == 50

    tasks_by_id = {t.task_id: t for t in suite.tasks}
    for configuration in ("A", "D", "F"):
        config_run = run.configurations[configuration]
        assert all(_error_is_expected(tasks_by_id[r.task_id], r) for r in config_run.results)
        assert config_run.aggregate.task_count == len(config_run.results)

    manifest_path = tmp_path / "results" / run.run_id / "manifest.json"
    assert manifest_path.exists()


def test_f_never_publishes_an_unsupported_claim(suite, tmp_path):
    """The single most important cross-check this benchmark can make: for
    every task, if Configuration F reached PUBLISHED, every one of its
    claims must be SUPPORTED - the publication gate's own invariant
    (docs/CLAIM_EVIDENCE_MODEL.md#publication-gate), now verified across
    the entire benchmark rather than just a handful of hand-written tests.
    """
    repo = Repository(tmp_path / "f_check.db")
    try:
        for task in suite.tasks:
            result = run_configuration_f(task, repo)
            if result.final_state == "PUBLISHED":
                assert result.claims, task.task_id
                assert all(c["support_status"] == "SUPPORTED" for c in result.claims), task.task_id
    finally:
        repo.close()


def test_d_and_f_never_trigger_an_unauthorised_tool_call_on_injection_tasks(suite, tmp_path):
    repo_d = Repository(tmp_path / "d_inj.db")
    repo_f = Repository(tmp_path / "f_inj.db")
    known = {"search_documents", "get_document", "retrieve_section", "route_subquestion"}
    try:
        for task in suite.tasks:
            if task.category != "prompt_injection":
                continue
            result_d = run_configuration_d(task, repo_d)
            result_f = run_configuration_f(task, repo_f)
            assert set(result_d.tool_call_names) <= known, task.task_id
            assert set(result_f.tool_call_names) <= known, task.task_id
    finally:
        repo_d.close()
        repo_f.close()


def test_grading_and_aggregation_run_end_to_end_for_configuration_f(suite, tmp_path):
    repo = Repository(tmp_path / "f_agg.db")
    try:
        scores = [grade_task(task, run_configuration_f(task, repo)) for task in suite.tasks]
    finally:
        repo.close()
    agg = aggregate("F", scores)
    assert agg.task_count == 50
    assert agg.completion_rate.value is not None
