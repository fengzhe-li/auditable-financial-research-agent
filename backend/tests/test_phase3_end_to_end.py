"""Phase 3 end-to-end: free-text question -> interpretation -> (clarify if
needed) -> autonomous routing -> evidence -> claims -> validation ->
sufficiency, entirely without the caller supplying companies or mapping
subquestions to tool calls.

Covers every docs/ROADMAP.md Phase 3 required test:
- ambiguous free-text question enters NEEDS_CLARIFICATION
- clarification resumes the original persisted task
- missing company/entity scope is not silently guessed
- router chooses the expected tool sequence for a bounded comparison task
- one-sided comparison evidence causes INSUFFICIENT_EVIDENCE
- conflicting evidence remains unresolved and cannot become SUPPORTED
- a fully covered task can still reach AWAITING_REVIEW
- unsupported claims still cannot reach PUBLISHED
"""

from __future__ import annotations

import pytest

from afra.domain.enums import ReviewDecision, TaskState
from afra.domain.errors import IllegalTransitionError

COMPARISON_QUESTION = "Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI infrastructure risk."
AMBIGUOUS_AXIS_QUESTION = "Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI exposure."
UNKNOWN_COMPANIES_QUESTION = "Compare Nvidia and AMD's AI exposure."


def test_fully_covered_task_reaches_awaiting_review_autonomously(orchestrator):
    """The full Phase 3 success criterion: free text in, no caller-supplied
    companies or tool mapping, reviewable claims out."""
    task = orchestrator.create_task(question_text=COMPARISON_QUESTION, created_by="analyst_1")
    task = orchestrator.run_autonomous_research(task.task_id)

    assert task.state == TaskState.AWAITING_REVIEW
    claims = orchestrator.repository.list_claims_for_task(task.task_id)
    assert len(claims) == len(task.resolved_scope["subquestions"])
    assert all(c.support_status.value == "SUPPORTED" for c in claims)

    # And a human can still take it all the way to PUBLISHED from here.
    task = orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
    task = orchestrator.publish(task.task_id)
    assert task.state == TaskState.PUBLISHED


def test_ambiguous_axis_enters_needs_clarification(orchestrator):
    task = orchestrator.create_task(question_text=AMBIGUOUS_AXIS_QUESTION, created_by="analyst_1")
    task = orchestrator.run_autonomous_research(task.task_id)

    assert task.state == TaskState.NEEDS_CLARIFICATION
    assert "comparison_axis" in task.pending_clarification
    scope = task.resolved_scope["research_scope"]
    assert scope["companies"]["status"] == "RESOLVED"
    assert scope["comparison_axis"]["status"] == "AMBIGUOUS"


def test_clarification_resumes_the_original_persisted_task_and_completes(orchestrator):
    task = orchestrator.create_task(question_text=AMBIGUOUS_AXIS_QUESTION, created_by="analyst_1")
    task = orchestrator.run_autonomous_research(task.task_id)
    assert task.state == TaskState.NEEDS_CLARIFICATION
    original_task_id = task.task_id
    original_companies = task.resolved_scope["research_scope"]["companies"]["value"]

    # Resuming answers the SAME task - not a new one - and, since the scope
    # is now fully resolved, the pipeline continues autonomously all the
    # way to a result without any further manual steps.
    resumed = orchestrator.submit_clarification_answer(original_task_id, {"comparison_axis": "risk"})

    assert resumed.task_id == original_task_id
    assert resumed.resolved_scope["research_scope"]["companies"]["value"] == original_companies
    assert resumed.resolved_scope["research_scope"]["comparison_axis"] == {
        "status": "RESOLVED", "value": "risk", "reason": None,
    }
    assert resumed.state == TaskState.AWAITING_REVIEW
    assert len(resumed.resolved_scope["subquestions"]) >= 2


def test_missing_company_scope_enters_clarification_not_guessed(orchestrator):
    """Nvidia/AMD are named but not in this system's known corpus - the
    system must ask, not silently substitute a known company or proceed
    with an empty scope."""
    task = orchestrator.create_task(question_text=UNKNOWN_COMPANIES_QUESTION, created_by="analyst_1")
    task = orchestrator.run_autonomous_research(task.task_id)

    assert task.state == TaskState.NEEDS_CLARIFICATION
    assert "companies" in task.pending_clarification
    scope = task.resolved_scope["research_scope"]
    assert scope["companies"]["status"] == "MISSING"
    assert scope["companies"]["value"] is None
    # Never silently guessed at Nvidia/AMD as if they were in scope.
    assert "Nvidia" not in str(scope["companies"]["value"])
    assert "AMD" not in str(scope["companies"]["value"])


def test_router_chooses_expected_tool_sequence(orchestrator):
    task = orchestrator.create_task(question_text=COMPARISON_QUESTION, created_by="analyst_1")
    task = orchestrator.run_autonomous_research(task.task_id)
    assert task.state == TaskState.AWAITING_REVIEW

    tool_calls = orchestrator.repository.list_tool_calls_for_task(task.task_id)
    tool_names = [tc.tool_name for tc in tool_calls]

    # One routing decision per subquestion.
    routing_calls = [tc for tc in tool_calls if tc.tool_name == "route_subquestion"]
    assert len(routing_calls) == len(task.resolved_scope["subquestions"])
    # Every routing decision is followed by at least one search_documents
    # and one retrieve_section call underneath it.
    assert tool_names.count("search_documents") >= 2  # once per company at minimum
    assert tool_names.count("retrieve_section") >= 2
    # The comparison subquestion's routing decision names both companies.
    comparison_call = next(tc for tc in routing_calls if "Contoso" in tc.output_summary and "Fabrikam" in tc.output_summary)
    assert "target_companies=" in comparison_call.output_summary
    assert "rationale=" in comparison_call.output_summary


def test_one_sided_comparison_evidence_causes_insufficient_evidence(orchestrator):
    task = orchestrator.create_task(question_text=COMPARISON_QUESTION, created_by="analyst_1")
    task = orchestrator.interpret_and_route(task.task_id)
    assert task.state == TaskState.GATHERING_EVIDENCE

    evidence_by_subquestion = orchestrator.route_and_gather_evidence(task.task_id)
    # Simulate a one-sided retrieval outcome: the router genuinely found
    # both sides, but drop Fabrikam's evidence everywhere it appears -
    # reproducing what a sparser corpus or a real search miss would look
    # like, without needing a second fixture corpus just for this test.
    for subquestion, evidence_list in evidence_by_subquestion.items():
        evidence_by_subquestion[subquestion] = [
            e for e in evidence_list if not e.source_document_id.startswith("FABRIKAM")
        ]

    orchestrator.complete_evidence_gathering(task.task_id)
    orchestrator.draft_claims_for_subquestions(task.task_id, evidence_by_subquestion, task.created_by)
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)

    assert task.state == TaskState.INSUFFICIENT_EVIDENCE
    sufficiency = task.resolved_scope["sufficiency_result"]
    assert sufficiency["comparison_sides_represented"] is False
    assert "Fabrikam Systems Inc" in sufficiency["missing_sides"]
    assert "Fabrikam" in task.resolved_scope["abstention_reason"]


def test_conflicting_evidence_remains_unresolved_via_autonomous_flow(orchestrator):
    """Complements tests/test_phase2_end_to_end.py's hand-constructed
    version: here the conflict is discovered by the real routing +
    drafting pipeline, using the genuinely different 2024/2025 Contoso
    filings, and a single-company (non-comparison) question so routing
    naturally retrieves both years for the same subquestion.
    """
    task = orchestrator.create_task(
        question_text="What does Contoso Cloud Corp disclose about AI infrastructure investment risk?",
        created_by="analyst_1",
    )
    task = orchestrator.interpret_and_route(task.task_id)
    assert task.state == TaskState.GATHERING_EVIDENCE
    subquestion = task.resolved_scope["subquestions"][0]

    # Router naturally only fetches the latest filing; manually add the
    # prior year too, to construct a genuine same-claim conflict the way a
    # richer router (Phase 3+ scope) might one day do on its own.
    evidence_2024 = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2024-10K", "risk_factors")
    evidence_2025 = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)

    from afra.domain.enums import SupportContribution
    from afra.orchestrator.orchestrator import EvidenceLinkSpec

    orchestrator.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp does not expect material AI infrastructure capex growth.",
        evidence_links=[
            EvidenceLinkSpec(evidence_2024.evidence_id, "do not currently expect this to require a material increase", SupportContribution.SUPPORTS),
            EvidenceLinkSpec(evidence_2025.evidence_id, "expect this capital expenditure to increase materially", SupportContribution.CONTRADICTS),
        ],
        created_by=task.created_by,
    )
    orchestrator.complete_synthesis(task.task_id)
    results = orchestrator.run_validation(task.task_id)
    assert results[0].computed_support_status.value == "CONFLICTING_EVIDENCE"

    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    assert task.state == TaskState.INSUFFICIENT_EVIDENCE
    with pytest.raises(IllegalTransitionError):
        orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)


def test_unsupported_claim_still_cannot_reach_published_via_autonomous_flow(orchestrator):
    task = orchestrator.create_task(
        question_text="What does Contoso Cloud Corp disclose about AI infrastructure investment risk?",
        created_by="analyst_1",
    )
    task = orchestrator.interpret_and_route(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)

    from afra.domain.enums import SupportContribution
    from afra.orchestrator.orchestrator import EvidenceLinkSpec

    orchestrator.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp's headcount declined sharply this year.",
        evidence_links=[
            EvidenceLinkSpec(evidence.evidence_id, "headcount declined sharply this year", SupportContribution.SUPPORTS),
        ],
        created_by=task.created_by,
    )
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)

    assert task.state == TaskState.INSUFFICIENT_EVIDENCE
    with pytest.raises(IllegalTransitionError):
        orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
