"""Asserts assemble_trace() covers every category persisted through the
full autonomous Phase 3 pipeline: interpreted scope, plan, routing
decisions, tool calls, evidence, drafted claims, validation results,
sufficiency result, and state transitions.
"""

from __future__ import annotations

from afra.domain.enums import ReviewDecision
from afra.trace import assemble_trace

QUESTION = (
    "Compare how Contoso Cloud Corp and Fabrikam Systems Inc describe AI "
    "infrastructure investment risk across their latest two annual reports."
)


def test_trace_covers_every_required_category(orchestrator):
    task = orchestrator.create_task(question_text=QUESTION, created_by="analyst_1")
    task = orchestrator.run_autonomous_research(task.task_id)
    assert task.state.value == "AWAITING_REVIEW"

    orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
    orchestrator.publish(task.task_id)

    trace = assemble_trace(task.task_id, orchestrator.repository)

    assert trace["interpreted_scope"]["companies"]["status"] == "RESOLVED"
    assert set(trace["interpreted_scope"]["companies"]["value"]) == {
        "Contoso Cloud Corp",
        "Fabrikam Systems Inc",
    }
    assert trace["unresolved_fields"] is None  # no clarification was needed
    assert trace["clarification_responses"] == []
    assert len(trace["plan"]) >= 2
    assert len(trace["routing_decisions"]) == len(trace["plan"])
    assert len(trace["tool_calls"]) >= len(trace["plan"])  # at least one search/retrieve per subquestion
    assert len(trace["retrieved_evidence"]) >= 2  # both companies represented
    assert len(trace["draft_claims"]) == len(trace["plan"])
    assert all(c["support_status"] == "SUPPORTED" for c in trace["draft_claims"])
    assert trace["sufficiency_result"]["is_sufficient"] is True
    assert trace["sufficiency_result"]["comparison_sides_represented"] is True
    assert trace["abstention_reason"] is None
    assert trace["final_status"] == "PUBLISHED"
    assert trace["state_transitions"][0] == {"from_state": "CREATED", "to_state": "PLANNING"}
    assert trace["state_transitions"][-1] == {"from_state": "APPROVED", "to_state": "PUBLISHED"}
    assert len(trace["reviews"]) == 1


def test_list_tasks_returns_every_task_most_recent_first(orchestrator):
    """Phase 7 addition (Repository.list_tasks()) - the read layer had no
    way to enumerate tasks before this."""
    first = orchestrator.create_task(question_text="What does Contoso Cloud Corp disclose?", created_by="analyst_1")
    second = orchestrator.create_task(question_text="What does Fabrikam Systems Inc disclose?", created_by="analyst_1")

    tasks = orchestrator.repository.list_tasks()

    assert [t.task_id for t in tasks] == [second.task_id, first.task_id]
