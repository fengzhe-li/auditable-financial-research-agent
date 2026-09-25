"""Phase 2 vertical slice: planner -> tools -> evidence-driven claim
drafting -> validation -> review -> publication, for the bounded
representative task:

    "Compare how Contoso Cloud Corp and Fabrikam Systems Inc describe AI
    infrastructure investment risk across their latest two annual reports."

Includes the Phase 2 failure cases from docs/ROADMAP.md:
- planner returns no usable subquestions -> safe failure
- retrieval returns no relevant evidence -> INSUFFICIENT_EVIDENCE
- a drafted claim with no evidence link -> validation blocks it
- conflicting evidence does not silently become SUPPORTED
- unsupported claims still cannot reach PUBLISHED
"""

from __future__ import annotations

import pytest

from afra.domain.enums import ReviewDecision, SupportContribution, TaskState
from afra.domain.errors import IllegalTransitionError, PlanningError
from afra.orchestrator.orchestrator import EvidenceLinkSpec

KNOWN_COMPANIES = ["Contoso Cloud Corp", "Fabrikam Systems Inc"]
QUESTION = (
    "Compare how Contoso Cloud Corp and Fabrikam Systems Inc describe AI "
    "infrastructure investment risk across their latest two annual reports."
)


def test_full_bounded_comparison_task_reaches_published(orchestrator):
    task = orchestrator.create_task(question_text=QUESTION, created_by="analyst_1")
    assert task.state == TaskState.PLANNING

    subquestions = orchestrator.run_planning(task.task_id, KNOWN_COMPANIES)
    assert 2 <= len(subquestions) <= 4
    task = orchestrator.repository.get_task(task.task_id)
    assert task.state == TaskState.GATHERING_EVIDENCE
    assert task.resolved_scope["subquestions"] == subquestions

    # search_documents() finds both filing years per company; the caller
    # (standing in for a future controller - see orchestrator module
    # docstring) picks the latest one for the single-company subquestions.
    contoso_docs = orchestrator.search_documents(task.task_id, company="Contoso Cloud Corp")
    fabrikam_docs = orchestrator.search_documents(task.task_id, company="Fabrikam Systems Inc")
    # Subset, not exact-equality: Phase 4 added non-10-K fixture documents
    # for both companies (see tools/fixtures.py) - both filing years must
    # still be found, but they are no longer the only documents returned.
    assert {"CONTOSO-2024-10K", "CONTOSO-2025-10K"} <= {d.document_id for d in contoso_docs}
    assert {"FABRIKAM-2024-10K", "FABRIKAM-2025-10K"} <= {d.document_id for d in fabrikam_docs}

    contoso_evidence = orchestrator.gather_evidence_from_document(
        task.task_id, "CONTOSO-2025-10K", "risk_factors"
    )
    fabrikam_evidence = orchestrator.gather_evidence_from_document(
        task.task_id, "FABRIKAM-2025-10K", "risk_factors"
    )
    orchestrator.complete_evidence_gathering(task.task_id)
    task = orchestrator.repository.get_task(task.task_id)
    assert task.state == TaskState.SYNTHESISING

    orchestrator.draft_claim_from_evidence(
        task.task_id, subquestions[0], [contoso_evidence], created_by="analyst_1"
    )
    orchestrator.draft_claim_from_evidence(
        task.task_id, subquestions[1], [fabrikam_evidence], created_by="analyst_1"
    )
    orchestrator.draft_claim_from_evidence(
        task.task_id, subquestions[2], [contoso_evidence, fabrikam_evidence], created_by="analyst_1"
    )
    orchestrator.complete_synthesis(task.task_id)
    task = orchestrator.repository.get_task(task.task_id)
    assert task.state == TaskState.VALIDATING

    results = orchestrator.run_validation(task.task_id)
    assert len(results) == 3
    assert all(r.computed_support_status.value == "SUPPORTED" for r in results)

    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    assert task.state == TaskState.AWAITING_REVIEW

    task = orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
    assert task.state == TaskState.APPROVED

    task = orchestrator.publish(task.task_id)
    assert task.state == TaskState.PUBLISHED

    # Cross-document, multi-evidence comparison claim genuinely cited both
    # companies' evidence.
    claims = orchestrator.repository.list_claims_for_task(task.task_id)
    comparison_claim = claims[2]
    links = orchestrator.repository.list_links_for_claim(comparison_claim.claim_id)
    assert {link.evidence_id for link in links} == {contoso_evidence.evidence_id, fabrikam_evidence.evidence_id}


def test_planner_safe_failure_fails_the_task(orchestrator):
    task = orchestrator.create_task(
        question_text="Compare Nvidia and AMD's AI exposure.", created_by="analyst_1"
    )

    with pytest.raises(PlanningError):
        orchestrator.run_planning(task.task_id, known_companies=[])  # underspecified

    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert "planning failed" in reloaded.resolved_scope["failure_reason"]


def test_retrieval_with_no_relevant_evidence_reaches_insufficient_evidence(orchestrator):
    task = orchestrator.create_task(question_text=QUESTION, created_by="analyst_1")
    subquestions = orchestrator.run_planning(task.task_id, KNOWN_COMPANIES)

    # Simulate a subquestion whose search turns up nothing relevant.
    results = orchestrator.search_documents(task.task_id, company="Northwind Traders")
    assert results == []

    orchestrator.complete_evidence_gathering(task.task_id)  # zero evidence allowed through
    orchestrator.draft_claim_without_evidence(task.task_id, subquestions[0], created_by="analyst_1")
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)

    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    assert task.state == TaskState.INSUFFICIENT_EVIDENCE

    claims = orchestrator.repository.list_claims_for_task(task.task_id)
    assert claims[0].support_status.value == "INSUFFICIENT_EVIDENCE"


def test_claim_with_no_evidence_link_is_blocked_by_validation(orchestrator):
    task = orchestrator.create_task(question_text=QUESTION, created_by="analyst_1")
    orchestrator.run_planning(task.task_id, KNOWN_COMPANIES)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)

    # Drafted with evidence_links=[] despite evidence having been retrieved -
    # e.g. a claim that got disconnected from its supporting citation.
    orchestrator.draft_claim(
        task.task_id, prompt="Contoso Cloud Corp faces AI infrastructure risk.",
        evidence_links=[], created_by="analyst_1",
    )
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)

    assert task.state == TaskState.INSUFFICIENT_EVIDENCE
    with pytest.raises(IllegalTransitionError):
        # Can't even get to submit_review from here without going back
        # through the documented resume path - proves there's no shortcut.
        orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)


def test_conflicting_evidence_does_not_become_supported(orchestrator):
    task = orchestrator.create_task(question_text=QUESTION, created_by="analyst_1")
    orchestrator.run_planning(task.task_id, KNOWN_COMPANIES)

    evidence_2024 = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2024-10K", "risk_factors")
    evidence_2025 = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)

    # A claim that the 2024 evidence supports and the 2025 evidence
    # genuinely contradicts (2024: "do not currently expect a material
    # increase"; 2025: "expect ... to increase materially").
    orchestrator.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp does not expect material AI infrastructure capex growth.",
        evidence_links=[
            EvidenceLinkSpec(
                evidence_id=evidence_2024.evidence_id,
                quote_span="do not currently expect this to require a material increase",
                support_contribution=SupportContribution.SUPPORTS,
            ),
            EvidenceLinkSpec(
                evidence_id=evidence_2025.evidence_id,
                quote_span="expect this capital expenditure to increase materially",
                support_contribution=SupportContribution.CONTRADICTS,
            ),
        ],
        created_by="analyst_1",
    )
    orchestrator.complete_synthesis(task.task_id)
    results = orchestrator.run_validation(task.task_id)

    assert results[0].computed_support_status.value == "CONFLICTING_EVIDENCE"

    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    # Phase 3 change (recorded in docs/ROADMAP.md): unresolved conflicting
    # evidence now blocks the sufficiency gate itself, routing to
    # INSUFFICIENT_EVIDENCE rather than passing through to AWAITING_REVIEW
    # as it did in Phase 2 - an autonomously discovered contradiction
    # should abstain, not be handed to a reviewer as a normal draft. It
    # remains true, either way, that a CONFLICTING_EVIDENCE claim can never
    # reach PUBLISHED - this test now proves that via the sufficiency gate
    # instead of the publication gate.
    assert task.state == TaskState.INSUFFICIENT_EVIDENCE
    assert "conflicting evidence" in task.resolved_scope["abstention_reason"]
    with pytest.raises(IllegalTransitionError):
        orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)


def test_unsupported_claim_still_cannot_reach_published(orchestrator):
    task = orchestrator.create_task(question_text=QUESTION, created_by="analyst_1")
    orchestrator.run_planning(task.task_id, KNOWN_COMPANIES)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)

    orchestrator.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp's headcount declined sharply this year.",
        evidence_links=[
            EvidenceLinkSpec(
                evidence_id=evidence.evidence_id,
                quote_span="headcount declined sharply this year",  # not present in the evidence
                support_contribution=SupportContribution.SUPPORTS,
            )
        ],
        created_by="analyst_1",
    )
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)

    assert task.state == TaskState.INSUFFICIENT_EVIDENCE
    claims = orchestrator.repository.list_claims_for_task(task.task_id)
    assert claims[0].support_status.value == "UNSUPPORTED"
