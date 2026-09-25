"""Phase 5 vertical slice: real human review, approval, revision, and
publication workflow - exercised through the orchestrator's public API and
real SQLite persistence, plus direct repository construction for the
defence-in-depth gate clauses that have no live orchestrator path (the same
pattern already used in test_publication_gate.py and
test_phase4_end_to_end.py for their own not-yet-reachable clauses).

Phase 5 success criterion: "AI-generated research can only become final
institutional output after explicit authorized review of the exact
validated version being published."
"""

from __future__ import annotations

import pytest

from afra.domain.enums import (
    Classification,
    DLPAction,
    ReviewDecision,
    SupportContribution,
    SupportStatus,
    TaskState,
)
from afra.domain.errors import (
    IllegalTransitionError,
    PublicationGateError,
    SecurityPolicyError,
    SelfApprovalError,
    UnauthorizedReviewerError,
)
from afra.domain.models import Claim, Review, ResearchTask, SecurityEvent, ValidationResult
from afra.orchestrator.orchestrator import EvidenceLinkSpec, ResearchTaskOrchestrator
from afra.providers.test_double import TestDoubleProvider
from afra.review.publication_gate import evaluate_publication_gate
from afra.storage.repository import Repository


def _run_to_awaiting_review(orchestrator, *, quote_span="increase materially"):
    task = orchestrator.create_task(
        question_text="What AI infrastructure investment risk does Contoso Cloud Corp disclose?",
        created_by="analyst_1",
    )
    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)
    orchestrator.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp expects AI infrastructure capex to increase materially.",
        evidence_links=[EvidenceLinkSpec(evidence.evidence_id, quote_span, SupportContribution.SUPPORTS)],
        created_by="analyst_1",
    )
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    assert task.state == TaskState.AWAITING_REVIEW
    return task, evidence


# -- 1. valid reviewer can approve a reviewable task --------------------------


def test_valid_reviewer_can_approve_a_reviewable_task(orchestrator):
    task, _ = _run_to_awaiting_review(orchestrator)
    task = orchestrator.submit_review(
        task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE, comments="Looks correct."
    )
    assert task.state == TaskState.APPROVED

    reviews = orchestrator.repository.list_reviews_for_task(task.task_id)
    assert len(reviews) == 1
    assert reviews[0].reviewer_id == "reviewer_1"
    assert reviews[0].decision == ReviewDecision.APPROVE
    assert reviews[0].comments == "Looks correct."
    assert reviews[0].claim_set_version == task.claim_set_version == 1
    # One ALLOW SecurityEvent always exists - every claim-drafting call goes
    # through Phase 4 enforcement, even for ordinary PUBLIC content.
    assert reviews[0].security_context == "1 security event(s) recorded: ['allow']"


# -- 2. self-approval / self-review is rejected --------------------------------


def test_self_approval_is_rejected(orchestrator):
    task, _ = _run_to_awaiting_review(orchestrator)
    with pytest.raises(SelfApprovalError):
        orchestrator.submit_review(task.task_id, reviewer_id="analyst_1", decision=ReviewDecision.APPROVE)
    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.AWAITING_REVIEW


def test_self_review_is_rejected_for_every_decision_not_only_approve(orchestrator):
    """Phase 5 strengthening: separation of duties applies to REJECT and
    REQUEST_REVISION too, not only APPROVE - see submit_review's docstring.
    """
    for decision in (ReviewDecision.REJECT, ReviewDecision.REQUEST_REVISION):
        task, _ = _run_to_awaiting_review(orchestrator)
        with pytest.raises(SelfApprovalError):
            orchestrator.submit_review(task.task_id, reviewer_id="analyst_1", decision=decision)


def test_unauthorized_reviewer_identity_is_rejected(orchestrator):
    task, _ = _run_to_awaiting_review(orchestrator)
    with pytest.raises(UnauthorizedReviewerError):
        orchestrator.submit_review(task.task_id, reviewer_id="random_person", decision=ReviewDecision.APPROVE)
    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.AWAITING_REVIEW


# -- 3. rejected task cannot publish -------------------------------------------


def test_rejected_task_cannot_publish(orchestrator):
    task, _ = _run_to_awaiting_review(orchestrator)
    task = orchestrator.submit_review(
        task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.REJECT, comments="Not acceptable."
    )
    assert task.state == TaskState.REJECTED

    with pytest.raises(PublicationGateError):
        orchestrator.publish(task.task_id)

    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.REJECTED  # unchanged, never silently published


# -- 4/5. REQUEST_REVISION resumes the same task; revised content requires ----
#         a new approval --------------------------------------------------------


def test_request_revision_resumes_same_task_and_revised_content_requires_new_approval(orchestrator):
    task, evidence = _run_to_awaiting_review(orchestrator)
    original_task_id = task.task_id

    task = orchestrator.submit_review(
        task.task_id,
        reviewer_id="reviewer_1",
        decision=ReviewDecision.REQUEST_REVISION,
        comments="Please add a second corroborating citation.",
    )
    assert task.state == TaskState.REVISION_REQUESTED
    assert task.task_id == original_task_id  # same task, not a new one

    # Resume the SAME task via the real revision-resume entry point: back
    # into SYNTHESISING (evidence was adequate; only drafting needed
    # changes), draft an additional claim (standing in for "the
    # revision"), and re-run the pipeline.
    task = orchestrator.resume_from_revision(task.task_id, needs_new_evidence=False)
    assert task.state == TaskState.SYNTHESISING
    assert task.task_id == original_task_id
    orchestrator.draft_claim(
        task.task_id,
        prompt="Additional corroborating context on Contoso Cloud Corp's AI capex plans.",
        evidence_links=[EvidenceLinkSpec(evidence.evidence_id, "increase materially", SupportContribution.SUPPORTS)],
        created_by="analyst_1",
    )
    task = orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    assert task.state == TaskState.AWAITING_REVIEW
    assert task.task_id == original_task_id
    assert task.claim_set_version == 2  # bumped by the second complete_synthesis()

    task = orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
    assert task.state == TaskState.APPROVED
    task = orchestrator.publish(task.task_id)
    assert task.state == TaskState.PUBLISHED

    reviews = orchestrator.repository.list_reviews_for_task(task.task_id)
    assert [r.decision for r in reviews] == [ReviewDecision.REQUEST_REVISION, ReviewDecision.APPROVE]
    assert reviews[0].claim_set_version == 1  # the version reviewed pre-revision
    assert reviews[1].claim_set_version == 2  # the version actually approved


# -- 6. stale approval cannot authorize a modified claim set ------------------


def test_stale_approval_cannot_authorize_a_modified_claim_set(repository):
    """Hand-constructed, like test_publication_gate.py's other
    defence-in-depth clauses: there is no live orchestrator path from
    APPROVED back to a modified claim set (APPROVED only transitions to
    PUBLISHED - see docs/STATE_MACHINE.md), so this proves the gate itself
    refuses a version mismatch directly against persisted state.
    """
    task = ResearchTask(question_text="q", created_by="analyst_1")
    task.state = TaskState.APPROVED
    task.claim_set_version = 2  # the claim set was modified after approval
    repository.save_task(task)

    claim = Claim(task_id=task.task_id, claim_text="c", model_id="test", created_by="analyst_1")
    repository.save_claim(claim)
    repository.record_validation_result(
        ValidationResult(claim_id=claim.claim_id, computed_support_status=SupportStatus.SUPPORTED)
    )
    repository.save_review(
        Review(
            task_id=task.task_id,
            reviewer_id="reviewer_1",
            decision=ReviewDecision.APPROVE,
            claim_set_version=1,  # reviewed version 1, task is now at version 2
        )
    )

    result = evaluate_publication_gate(task.task_id, repository)
    assert not result.passed
    assert any("stale" in r for r in result.reasons)

    orchestrator = ResearchTaskOrchestrator(repository, TestDoubleProvider())
    with pytest.raises(PublicationGateError):
        orchestrator.publish(task.task_id)


# -- 7/8/9. existing invariants: unsupported / insufficient evidence / -------
#           conflicting evidence still block publication --------------------
# (already directly covered by test_publication_gate.py and
# test_end_to_end_slice.py / test_phase2_end_to_end.py; not duplicated here
# to preserve those as the single source of truth for those invariants -
# see this file's module docstring and item 10's "preserve all existing
# tests.")


# -- 10. SECURITY_BLOCKED cannot be bypassed by ordinary approval -------------


def test_security_blocked_task_cannot_be_reviewed_or_approved(orchestrator):
    task = orchestrator.create_task(
        question_text="What does Contoso Cloud Corp disclose?", created_by="analyst_1"
    )
    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-MNPI-NOTE", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)

    with pytest.raises(SecurityPolicyError):
        orchestrator.draft_claim(
            task.task_id,
            prompt="Summarise the restricted note.",
            evidence_links=[
                EvidenceLinkSpec(evidence.evidence_id, "material non-public information", SupportContribution.SUPPORTS)
            ],
            created_by="analyst_1",
        )
    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.SECURITY_BLOCKED

    # An ordinary reviewer cannot approve (or review at all) a task that
    # never reached AWAITING_REVIEW - the state machine itself is the
    # enforcement, not a special-cased check in submit_review().
    with pytest.raises(IllegalTransitionError):
        orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)

    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.SECURITY_BLOCKED  # unchanged


def test_resolving_a_require_approval_security_event_does_not_revive_a_blocked_task(repository):
    """Phase 5's real resolution workflow (governance sign-off on a
    require_approval event) is exercised here - and proven NOT to be an
    override: the task stays SECURITY_BLOCKED, and there is still no path
    to APPROVED/PUBLISHED for it, because SECURITY_BLOCKED has no outgoing
    transitions at all (docs/STATE_MACHINE.md). No override path is
    invented - see afra.orchestrator.orchestrator.resolve_security_event.
    """
    orchestrator = ResearchTaskOrchestrator(repository, TestDoubleProvider())
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-BOARD-MEMO", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)

    with pytest.raises(SecurityPolicyError):
        orchestrator.draft_claim(
            task.task_id,
            prompt="Please reference account ACCT-77001 regarding the draft figures.",
            evidence_links=[
                EvidenceLinkSpec(evidence.evidence_id, "Draft, unpublished figures", SupportContribution.SUPPORTS)
            ],
            created_by="analyst_1",
        )
    events = repository.list_security_events_for_task(task.task_id)
    assert events[0].action_taken == DLPAction.REQUIRE_APPROVAL
    assert events[0].resolved_by is None

    resolved = orchestrator.resolve_security_event(events[0].security_event_id, resolved_by="governance_admin")
    assert resolved.resolved_by == "governance_admin"

    # Still SECURITY_BLOCKED - resolving the event is an audit action, not
    # an unblock.
    reloaded = repository.get_task(task.task_id)
    assert reloaded.state == TaskState.SECURITY_BLOCKED
    with pytest.raises(IllegalTransitionError):
        orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)


def test_only_require_approval_security_events_can_be_resolved_not_block(orchestrator):
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-MNPI-NOTE", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)
    with pytest.raises(SecurityPolicyError):
        orchestrator.draft_claim(
            task.task_id,
            prompt="Summarise the restricted note.",
            evidence_links=[
                EvidenceLinkSpec(evidence.evidence_id, "material non-public information", SupportContribution.SUPPORTS)
            ],
            created_by="analyst_1",
        )
    events = orchestrator.repository.list_security_events_for_task(task.task_id)
    assert events[0].action_taken == DLPAction.BLOCK

    with pytest.raises(SecurityPolicyError):
        orchestrator.resolve_security_event(events[0].security_event_id, resolved_by="governance_admin")


def test_only_governance_identity_can_resolve_a_security_event(repository):
    # Only the external provider registered, so CONFIDENTIAL content with a
    # detected sensitive category has no eligible fallback -> REQUIRE_APPROVAL
    # (same construction as test_resolving_a_require_approval_security_event_does_not_revive_a_blocked_task).
    orchestrator = ResearchTaskOrchestrator(repository, TestDoubleProvider())
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-BOARD-MEMO", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)
    with pytest.raises(SecurityPolicyError):
        orchestrator.draft_claim(
            task.task_id,
            prompt="Please reference account ACCT-77001 regarding the draft figures.",
            evidence_links=[
                EvidenceLinkSpec(evidence.evidence_id, "Draft, unpublished figures", SupportContribution.SUPPORTS)
            ],
            created_by="analyst_1",
        )
    events = orchestrator.repository.list_security_events_for_task(task.task_id)
    with pytest.raises(UnauthorizedReviewerError):
        orchestrator.resolve_security_event(events[0].security_event_id, resolved_by="reviewer_1")


# -- 11. approved, validated, policy-clean task can reach PUBLISHED -----------


def test_approved_validated_policy_clean_task_reaches_published(orchestrator):
    task, _ = _run_to_awaiting_review(orchestrator)
    task = orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
    assert task.state == TaskState.APPROVED
    task = orchestrator.publish(task.task_id)
    assert task.state == TaskState.PUBLISHED


# -- 12. review history persists across process restart -----------------------


def test_review_history_persists_across_process_restart(db_path):
    repo_1 = Repository(db_path)
    orch_1 = ResearchTaskOrchestrator(repo_1, TestDoubleProvider())
    task, evidence = _run_to_awaiting_review(orch_1)
    orch_1.submit_review(
        task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.REQUEST_REVISION, comments="Add more detail."
    )
    orch_1.resume_from_revision(task.task_id, needs_new_evidence=False)
    orch_1.draft_claim(
        task.task_id,
        prompt="Revised claim text with more detail.",
        evidence_links=[EvidenceLinkSpec(evidence.evidence_id, "increase materially", SupportContribution.SUPPORTS)],
        created_by="analyst_1",
    )
    orch_1.complete_synthesis(task.task_id)
    orch_1.run_validation(task.task_id)
    task = orch_1.evaluate_sufficiency_and_advance(task.task_id)
    orch_1.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
    repo_1.close()

    repo_2 = Repository(db_path)
    reviews = repo_2.list_reviews_for_task(task.task_id)
    assert len(reviews) == 2
    assert reviews[0].decision == ReviewDecision.REQUEST_REVISION
    assert reviews[0].comments == "Add more detail."
    assert reviews[1].decision == ReviewDecision.APPROVE
    assert reviews[1].claim_set_version == 2
    reloaded = repo_2.get_task(task.task_id)
    assert reloaded.state == TaskState.APPROVED
    repo_2.close()
