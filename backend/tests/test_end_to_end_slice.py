"""End-to-end exercise of the Phase 1 vertical slice through the
orchestrator's public API: the full documented path from CREATED to
PUBLISHED, plus the two negative paths (unsupported claim, self-approval)
that must NOT reach PUBLISHED.

This complements, rather than replaces, the direct unit tests in
test_state_machine.py, test_validator.py, and test_publication_gate.py -
see docs/ROADMAP.md's note that the gate specifically must be unit-tested
directly, not only via this kind of happy-path test.
"""

from __future__ import annotations

import pytest

from afra.domain.enums import ReviewDecision, SupportContribution, SupportStatus, TaskState
from afra.domain.errors import PublicationGateError, SelfApprovalError
from afra.domain.models import Claim, Review, ResearchTask, ValidationResult
from afra.orchestrator.orchestrator import EvidenceLinkSpec


def _run_to_awaiting_review(orchestrator, *, claim_text_prompt, quote_span, contribution):
    task = orchestrator.create_task(
        question_text="What AI infrastructure investment risk does Contoso Cloud Corp disclose?",
        created_by="analyst_1",
    )

    orchestrator.request_clarification(
        task.task_id, "Which company and which filing year?"
    )
    orchestrator.submit_clarification_answer(
        task.task_id,
        {"company": "Contoso Cloud Corp", "document_id": "CONTOSO-2025-10K"},
    )

    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(
        task.task_id, "CONTOSO-2025-10K", "risk_factors"
    )
    orchestrator.complete_evidence_gathering(task.task_id)

    orchestrator.draft_claim(
        task.task_id,
        prompt=claim_text_prompt,
        evidence_links=[
            EvidenceLinkSpec(
                evidence_id=evidence.evidence_id,
                quote_span=quote_span,
                support_contribution=contribution,
            )
        ],
        created_by="analyst_1",
    )
    orchestrator.complete_synthesis(task.task_id)

    orchestrator.run_validation(task.task_id)
    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    return task


def test_full_happy_path_reaches_published(orchestrator):
    task = _run_to_awaiting_review(
        orchestrator,
        claim_text_prompt=(
            "Contoso Cloud Corp expects AI infrastructure capital expenditure to "
            "increase materially."
        ),
        quote_span="to increase materially in the next fiscal year",
        contribution=SupportContribution.SUPPORTS,
    )
    assert task.state == TaskState.AWAITING_REVIEW

    task = orchestrator.submit_review(
        task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE
    )
    assert task.state == TaskState.APPROVED

    task = orchestrator.publish(task.task_id)
    assert task.state == TaskState.PUBLISHED


def test_unsupported_claim_never_reaches_published(orchestrator):
    """The project's core invariant, exercised through the full orchestrator
    flow rather than a hand-constructed gate test: a claim whose cited quote
    does not appear in the evidence text is UNSUPPORTED, which blocks
    AWAITING_REVIEW->APPROVED from ever mattering, because even if a
    reviewer approves it, publish() still refuses.
    """
    task = _run_to_awaiting_review(
        orchestrator,
        claim_text_prompt="Contoso Cloud Corp's revenue declined sharply this year.",
        quote_span="revenue declined sharply this year",  # not present in the fixture text
        contribution=SupportContribution.SUPPORTS,
    )
    # The claim was UNSUPPORTED, so sufficiency gate should have kept it out
    # of AWAITING_REVIEW entirely.
    assert task.state == TaskState.INSUFFICIENT_EVIDENCE

    claims = orchestrator.repository.list_claims_for_task(task.task_id)
    assert claims[0].support_status.value == "UNSUPPORTED"


def test_insufficient_evidence_cannot_be_forced_to_published_via_review(orchestrator):
    """Even if a task somehow reaches AWAITING_REVIEW with a claim that
    isn't SUPPORTED (bypassing the sufficiency gate is not possible through
    the orchestrator's public API, but the publication gate is re-checked
    independently regardless of how AWAITING_REVIEW was reached - this test
    forces exactly that scenario directly against the repository to prove
    the gate, not just the sufficiency check, is what actually protects
    PUBLISHED).
    """
    repo = orchestrator.repository
    task = ResearchTask(question_text="q", created_by="analyst_1")
    task.state = TaskState.APPROVED
    repo.save_task(task)
    claim = Claim(task_id=task.task_id, claim_text="c", model_id="test", created_by="analyst_1")
    repo.save_claim(claim)
    repo.record_validation_result(
        ValidationResult(claim_id=claim.claim_id, computed_support_status=SupportStatus.INSUFFICIENT_EVIDENCE)
    )
    repo.save_review(Review(task_id=task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE))

    with pytest.raises(PublicationGateError):
        orchestrator.publish(task.task_id)

    reloaded = repo.get_task(task.task_id)
    assert reloaded.state == TaskState.APPROVED  # unchanged - never silently published


def test_self_approval_is_rejected(orchestrator):
    task = _run_to_awaiting_review(
        orchestrator,
        claim_text_prompt=(
            "Contoso Cloud Corp expects AI infrastructure capital expenditure to "
            "increase materially."
        ),
        quote_span="to increase materially in the next fiscal year",
        contribution=SupportContribution.SUPPORTS,
    )
    assert task.state == TaskState.AWAITING_REVIEW

    with pytest.raises(SelfApprovalError):
        orchestrator.submit_review(
            task.task_id, reviewer_id="analyst_1", decision=ReviewDecision.APPROVE
        )

    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.AWAITING_REVIEW  # unchanged


def test_model_call_audit_trail_is_recorded(orchestrator):
    task = _run_to_awaiting_review(
        orchestrator,
        claim_text_prompt="Contoso Cloud Corp AI infrastructure spend is increasing materially.",
        quote_span="to increase materially in the next fiscal year",
        contribution=SupportContribution.SUPPORTS,
    )
    calls = orchestrator.repository.list_model_calls_for_task(task.task_id)
    assert len(calls) == 1
    assert calls[0].purpose == "draft_claim"
    assert calls[0].provider_id == "test-double-external"
