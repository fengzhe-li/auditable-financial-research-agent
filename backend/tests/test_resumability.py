"""Proves NEEDS_CLARIFICATION resumes from persisted state after a genuine
process restart, not from in-memory state - docs/ROADMAP.md Phase 1's
fourth required test.

The "restart" is real, not simulated in name only: the first Orchestrator
and Repository instances are closed and discarded entirely, and a brand new
Repository (fresh sqlite3 connection) and Orchestrator are constructed
pointing at the same on-disk file. The second orchestrator has no Python
object in common with the first - if resumption worked only because of
in-memory state, this test would fail.
"""

from __future__ import annotations

from afra.domain.enums import TaskState
from afra.orchestrator.orchestrator import ResearchTaskOrchestrator
from afra.providers.test_double import TestDoubleProvider
from afra.storage.repository import Repository


def test_needs_clarification_resumes_after_process_restart(db_path):
    # --- "process 1": create the task and ask for clarification ---
    repo_1 = Repository(db_path)
    orchestrator_1 = ResearchTaskOrchestrator(repo_1, TestDoubleProvider())

    task = orchestrator_1.create_task(
        question_text="Compare Nvidia and AMD's AI exposure.",
        created_by="analyst_1",
    )
    task_id = task.task_id

    orchestrator_1.request_clarification(
        task_id,
        "Which dimension of AI exposure: revenue, capex, product, or risk disclosure?",
    )

    reloaded = repo_1.get_task(task_id)
    assert reloaded.state == TaskState.NEEDS_CLARIFICATION
    assert reloaded.pending_clarification

    # Simulate a genuine process restart: close everything, keep only the
    # task_id and the db path (what would actually survive a restart).
    repo_1.close()
    del repo_1
    del orchestrator_1

    # --- "process 2": a completely fresh Repository/Orchestrator ---
    repo_2 = Repository(db_path)
    orchestrator_2 = ResearchTaskOrchestrator(repo_2, TestDoubleProvider())

    resumed = orchestrator_2.submit_clarification_answer(
        task_id, {"exposure_dimension": "revenue", "time_period": "latest fiscal year"}
    )

    assert resumed.task_id == task_id
    assert resumed.state == TaskState.PLANNING
    assert resumed.pending_clarification is None
    assert resumed.resolved_scope["exposure_dimension"] == "revenue"
    assert resumed.resolved_scope["time_period"] == "latest fiscal year"

    # And the resumed state is itself durable, not just returned in memory.
    reloaded_again = repo_2.get_task(task_id)
    assert reloaded_again.state == TaskState.PLANNING
    assert reloaded_again.resolved_scope["exposure_dimension"] == "revenue"

    repo_2.close()


def test_resuming_does_not_lose_already_resolved_scope(db_path):
    """If only part of the scope was ambiguous, resuming must not discard
    scope fragments already agreed on before the clarification round-trip -
    docs/STATE_MACHINE.md#resumability.
    """
    repo_1 = Repository(db_path)
    orchestrator_1 = ResearchTaskOrchestrator(repo_1, TestDoubleProvider())

    task = orchestrator_1.create_task(
        question_text="Compare Nvidia and AMD's AI exposure.",
        created_by="analyst_1",
    )
    task_id = task.task_id

    # Company scope was already resolved before the ambiguity was found.
    task = repo_1.get_task(task_id)
    task.resolved_scope["companies"] = ["Nvidia", "AMD"]
    repo_1.save_task(task)

    orchestrator_1.request_clarification(task_id, "Which dimension of AI exposure?")
    repo_1.close()

    repo_2 = Repository(db_path)
    orchestrator_2 = ResearchTaskOrchestrator(repo_2, TestDoubleProvider())
    resumed = orchestrator_2.submit_clarification_answer(
        task_id, {"exposure_dimension": "capex"}
    )

    assert resumed.resolved_scope["companies"] == ["Nvidia", "AMD"]
    assert resumed.resolved_scope["exposure_dimension"] == "capex"
    repo_2.close()
