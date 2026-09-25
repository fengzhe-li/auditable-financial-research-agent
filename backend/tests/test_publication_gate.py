"""Direct unit tests of the publication gate - constructed by hand against
the repository, not via the orchestrator's full lifecycle, per
docs/ROADMAP.md's requirement that the gate be "unit-tested directly, not
only via end-to-end happy path."

This is the test file that proves the project's core invariant:
"An AI-generated claim unsupported by evidence cannot reach PUBLISHED."
"""

from __future__ import annotations

from afra.domain.enums import (
    ApprovalStatus,
    Classification,
    DLPAction,
    ReviewDecision,
    SupportStatus,
    TaskState,
)
from afra.domain.models import Claim, Review, ResearchTask, SecurityEvent, ValidationResult
from afra.review.publication_gate import evaluate_publication_gate


def _approved_task_with_claim(repository, support_status, *, reviewer_id="reviewer_1", created_by="analyst_1"):
    task = ResearchTask(question_text="q", created_by=created_by)
    task.state = TaskState.APPROVED
    repository.save_task(task)

    claim = Claim(task_id=task.task_id, claim_text="c", model_id="test", created_by=created_by)
    repository.save_claim(claim)
    repository.record_validation_result(
        ValidationResult(claim_id=claim.claim_id, computed_support_status=support_status)
    )

    repository.save_review(
        Review(task_id=task.task_id, reviewer_id=reviewer_id, decision=ReviewDecision.APPROVE)
    )
    return task, claim


def test_unsupported_claim_blocks_publication(repository):
    task, _ = _approved_task_with_claim(repository, SupportStatus.UNSUPPORTED)

    result = evaluate_publication_gate(task.task_id, repository)

    assert not result.passed
    assert any("UNSUPPORTED" in r for r in result.reasons)


def test_insufficient_evidence_claim_blocks_publication(repository):
    """A claim that never resolved past INSUFFICIENT_EVIDENCE must not be
    silently treated as a supported finding just because a reviewer clicked
    approve - the gate re-checks support_status independently of the review
    decision.
    """
    task, _ = _approved_task_with_claim(repository, SupportStatus.INSUFFICIENT_EVIDENCE)

    result = evaluate_publication_gate(task.task_id, repository)

    assert not result.passed
    assert any("INSUFFICIENT_EVIDENCE" in r for r in result.reasons)


def test_conflicting_evidence_claim_blocks_publication(repository):
    task, _ = _approved_task_with_claim(repository, SupportStatus.CONFLICTING_EVIDENCE)

    result = evaluate_publication_gate(task.task_id, repository)

    assert not result.passed


def test_partially_supported_claim_blocks_publication_in_phase_1(repository):
    """Phase 1 does not implement the documented "disclosed partial
    support" exception (no memo/section concept exists yet) - see
    afra.review.publication_gate module docstring - so
    PARTIALLY_SUPPORTED is conservatively rejected, not silently accepted.
    """
    task, _ = _approved_task_with_claim(repository, SupportStatus.PARTIALLY_SUPPORTED)

    result = evaluate_publication_gate(task.task_id, repository)

    assert not result.passed


def test_supported_claim_with_separated_reviewer_passes(repository):
    task, _ = _approved_task_with_claim(
        repository, SupportStatus.SUPPORTED, reviewer_id="reviewer_1", created_by="analyst_1"
    )

    result = evaluate_publication_gate(task.task_id, repository)

    assert result.passed
    assert result.reasons == []


def test_unauthorized_reviewer_identity_blocks_publication_even_if_claim_is_supported(repository):
    """Phase 5: 'reviewer is authorized' is a real, independently-checked
    gate clause, distinct from reviewer-separation (reviewer_id !=
    created_by) - see afra.identity.AUTHORIZED_REVIEWER_IDS.
    """
    task, _ = _approved_task_with_claim(
        repository, SupportStatus.SUPPORTED, reviewer_id="someone_not_registered", created_by="analyst_1"
    )

    result = evaluate_publication_gate(task.task_id, repository)

    assert not result.passed
    assert any("not an authorized reviewer identity" in r for r in result.reasons)


def test_self_approval_blocks_publication_even_if_claim_is_supported(repository):
    task, _ = _approved_task_with_claim(
        repository, SupportStatus.SUPPORTED, reviewer_id="analyst_1", created_by="analyst_1"
    )

    result = evaluate_publication_gate(task.task_id, repository)

    assert not result.passed
    assert any("self-approval" in r for r in result.reasons)


def test_task_not_approved_blocks_publication(repository):
    task = ResearchTask(question_text="q", created_by="analyst_1")
    task.state = TaskState.AWAITING_REVIEW
    repository.save_task(task)

    result = evaluate_publication_gate(task.task_id, repository)

    assert not result.passed
    assert any("not APPROVED" in r for r in result.reasons)


def test_unresolved_blocking_security_event_blocks_publication_even_if_claim_is_supported(repository):
    """Phase 4: the gate's SecurityEvent clause is real, not trivially
    satisfied - a task can reach this hand-constructed APPROVED state (e.g.
    via direct repository writes, as this test does) with an unresolved
    `block` SecurityEvent still on record, and the gate must still refuse
    it independently of support_status/review state. See
    afra.review.publication_gate's module docstring for why this path is
    not reachable via the orchestrator today, but is still checked here.
    """
    task, _ = _approved_task_with_claim(repository, SupportStatus.SUPPORTED)
    repository.save_security_event(
        SecurityEvent(
            task_id=task.task_id,
            trigger="draft_claim:model_routing",
            classification=Classification.RESTRICTED,
            action_taken=DLPAction.BLOCK,
        )
    )

    result = evaluate_publication_gate(task.task_id, repository)

    assert not result.passed
    assert any("SecurityEvent" in r for r in result.reasons)


def test_resolved_require_approval_security_event_does_not_block_publication(repository):
    task, _ = _approved_task_with_claim(repository, SupportStatus.SUPPORTED)
    repository.save_security_event(
        SecurityEvent(
            task_id=task.task_id,
            trigger="draft_claim:model_routing",
            classification=Classification.RESTRICTED,
            action_taken=DLPAction.REQUIRE_APPROVAL,
            resolved_by="governance_admin",
        )
    )

    result = evaluate_publication_gate(task.task_id, repository)

    assert result.passed


def test_no_approve_review_blocks_publication(repository):
    task = ResearchTask(question_text="q", created_by="analyst_1")
    task.state = TaskState.APPROVED
    repository.save_task(task)
    claim = Claim(task_id=task.task_id, claim_text="c", model_id="test", created_by="analyst_1")
    repository.save_claim(claim)
    repository.record_validation_result(
        ValidationResult(claim_id=claim.claim_id, computed_support_status=SupportStatus.SUPPORTED)
    )
    # No Review row saved at all.

    result = evaluate_publication_gate(task.task_id, repository)

    assert not result.passed
    assert any("no APPROVE review" in r for r in result.reasons)
