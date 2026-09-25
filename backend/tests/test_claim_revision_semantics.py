"""Claim revision-semantics tests - see
docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics.

Kept separate from test_phase5_review_workflow.py (which this module
builds directly on top of) matching this project's established convention
of one test file per coherent slice of behaviour. Exercises the four new
orchestrator primitives (replace_claim, supersede_claim, withdraw_claim,
object_to_claim), afra.domain.claim_lifecycle.compute_effective_claims,
and the propagation of all of that into run_validation/compute_sufficiency/
evaluate_publication_gate - all through real SQLite persistence, the same
pattern test_phase5_review_workflow.py uses.
"""

from __future__ import annotations

import pytest

from afra.domain.claim_lifecycle import compute_effective_claims
from afra.domain.enums import (
    ClaimLifecycleStatus,
    ReviewDecision,
    SupportContribution,
    SupportStatus,
    TaskState,
)
from afra.domain.errors import (
    ClaimNotFoundError,
    IllegalClaimRevisionError,
    PublicationGateError,
    SelfApprovalError,
    UnauthorizedReviewerError,
)
from afra.domain.models import Claim
from afra.orchestrator.orchestrator import EvidenceLinkSpec
from afra.review.publication_gate import evaluate_publication_gate


def _run_to_awaiting_review(orchestrator, *, quote_span="increase materially"):
    """Same shape as test_phase5_review_workflow.py's helper - a single
    ACTIVE claim, task in AWAITING_REVIEW."""
    task = orchestrator.create_task(
        question_text="What AI infrastructure investment risk does Contoso Cloud Corp disclose?",
        created_by="analyst_1",
    )
    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)
    claim = orchestrator.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp expects AI infrastructure capex to increase materially.",
        evidence_links=[EvidenceLinkSpec(evidence.evidence_id, quote_span, SupportContribution.SUPPORTS)],
        created_by="analyst_1",
    )
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    assert task.state == TaskState.AWAITING_REVIEW
    return task, evidence, claim


def _approve(orchestrator, task_id):
    return orchestrator.submit_review(task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)


# -- 1. replace ---------------------------------------------------------------


def test_replace_claim_creates_a_new_claim_and_marks_the_old_one_replaced(orchestrator):
    task, evidence, claim = _run_to_awaiting_review(orchestrator)

    new_claim = orchestrator.replace_claim(
        task.task_id,
        old_claim_id=claim.claim_id,
        claim_text="Corrected: Contoso Cloud Corp expects AI infrastructure capex to increase materially in FY2025.",
        created_by="analyst_1",
        evidence_links=[EvidenceLinkSpec(evidence.evidence_id, "increase materially", SupportContribution.SUPPORTS)],
    )

    old = orchestrator.repository.get_claim(claim.claim_id)
    assert old.lifecycle_status == ClaimLifecycleStatus.REPLACED
    assert old.successor_claim_id == new_claim.claim_id
    assert new_claim.predecessor_claim_id == claim.claim_id
    assert new_claim.lifecycle_status == ClaimLifecycleStatus.ACTIVE
    # replace_claim validates the new claim immediately - it does not route
    # back through VALIDATING (the task already left it).
    assert new_claim.support_status == SupportStatus.SUPPORTED


def test_replace_claim_bumps_claim_set_version_immediately(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)
    assert task.claim_set_version == 1

    orchestrator.replace_claim(task.task_id, old_claim_id=claim.claim_id, claim_text="corrected", created_by="analyst_1")

    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.claim_set_version == 2


def test_replace_claim_rejects_an_already_inactive_claim(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)
    orchestrator.replace_claim(task.task_id, old_claim_id=claim.claim_id, claim_text="v2", created_by="analyst_1")

    with pytest.raises(IllegalClaimRevisionError):
        orchestrator.replace_claim(task.task_id, old_claim_id=claim.claim_id, claim_text="v3", created_by="analyst_1")


def test_replace_claim_rejects_unknown_claim_id(orchestrator):
    task, _, _ = _run_to_awaiting_review(orchestrator)
    with pytest.raises(ClaimNotFoundError):
        orchestrator.replace_claim(task.task_id, old_claim_id="claim_does_not_exist", claim_text="x", created_by="analyst_1")


def test_replace_claim_rejects_a_claim_belonging_to_a_different_task(orchestrator):
    task_a, _, claim_a = _run_to_awaiting_review(orchestrator)
    task_b, _, _ = _run_to_awaiting_review(orchestrator)
    with pytest.raises(ClaimNotFoundError):
        orchestrator.replace_claim(task_b.task_id, old_claim_id=claim_a.claim_id, claim_text="x", created_by="analyst_1")


def test_replace_claim_refused_on_a_terminal_task(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)
    orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.REJECT)
    with pytest.raises(IllegalClaimRevisionError):
        orchestrator.replace_claim(task.task_id, old_claim_id=claim.claim_id, claim_text="x", created_by="analyst_1")


# -- 2. supersede ---------------------------------------------------------------


def test_supersede_claim_links_an_already_drafted_claim_and_does_not_bump_version(orchestrator):
    """supersede_claim is meant to be called mid-revision, between drafting
    a batch of new claims and calling complete_synthesis() once for the
    whole batch - so it must not bump claim_set_version itself."""
    task, evidence, old_claim = _run_to_awaiting_review(orchestrator)
    review = orchestrator.submit_review(
        task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.REQUEST_REVISION
    )
    assert review.state == TaskState.REVISION_REQUESTED
    task = orchestrator.resume_from_revision(task.task_id, needs_new_evidence=False)
    assert task.state == TaskState.SYNTHESISING

    new_claim = orchestrator.draft_claim(
        task.task_id,
        prompt="Revised claim with an additional citation.",
        evidence_links=[EvidenceLinkSpec(evidence.evidence_id, "increase materially", SupportContribution.SUPPORTS)],
        created_by="analyst_1",
    )

    before_version = orchestrator.repository.get_task(task.task_id).claim_set_version
    orchestrator.supersede_claim(task.task_id, old_claim_id=old_claim.claim_id, new_claim_id=new_claim.claim_id)
    after_version = orchestrator.repository.get_task(task.task_id).claim_set_version
    assert after_version == before_version  # not bumped by supersede_claim itself

    old = orchestrator.repository.get_claim(old_claim.claim_id)
    assert old.lifecycle_status == ClaimLifecycleStatus.SUPERSEDED
    assert old.successor_claim_id == new_claim.claim_id
    new = orchestrator.repository.get_claim(new_claim.claim_id)
    assert new.predecessor_claim_id == old_claim.claim_id

    # complete_synthesis() performs the one version bump for this batch, as
    # it always has.
    task = orchestrator.complete_synthesis(task.task_id)
    assert task.claim_set_version == 2


def test_supersede_claim_rejects_unknown_new_claim_id(orchestrator):
    task, _, old_claim = _run_to_awaiting_review(orchestrator)
    orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.REQUEST_REVISION)
    orchestrator.resume_from_revision(task.task_id, needs_new_evidence=False)
    with pytest.raises(ClaimNotFoundError):
        orchestrator.supersede_claim(task.task_id, old_claim_id=old_claim.claim_id, new_claim_id="nope")


# -- 3. withdraw ---------------------------------------------------------------


def test_withdraw_claim_removes_it_from_the_effective_set_with_a_reason_and_no_successor(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)

    reloaded_task = orchestrator.withdraw_claim(task.task_id, claim_id=claim.claim_id, reason="evidence was misread")

    withdrawn = orchestrator.repository.get_claim(claim.claim_id)
    assert withdrawn.lifecycle_status == ClaimLifecycleStatus.WITHDRAWN
    assert withdrawn.withdrawn_reason == "evidence was misread"
    assert withdrawn.successor_claim_id is None
    assert reloaded_task.claim_set_version == 2  # bumped, same as replace_claim


def test_withdraw_claim_rejects_an_already_inactive_claim(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)
    orchestrator.withdraw_claim(task.task_id, claim_id=claim.claim_id, reason="first withdrawal")
    with pytest.raises(IllegalClaimRevisionError):
        orchestrator.withdraw_claim(task.task_id, claim_id=claim.claim_id, reason="second attempt")


# -- 4. objection history -------------------------------------------------------


def test_object_to_claim_records_an_objection_without_changing_lifecycle_or_version(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)

    objection = orchestrator.object_to_claim(
        task.task_id, claim_id=claim.claim_id, reviewer_id="reviewer_1", reason="citation seems weak"
    )

    assert objection.claim_id == claim.claim_id
    assert objection.reviewer_id == "reviewer_1"
    assert objection.claim_set_version == 1

    unchanged = orchestrator.repository.get_claim(claim.claim_id)
    assert unchanged.lifecycle_status == ClaimLifecycleStatus.ACTIVE
    reloaded_task = orchestrator.repository.get_task(task.task_id)
    assert reloaded_task.claim_set_version == 1  # not bumped by an objection alone


def test_objection_history_accumulates_multiple_entries_and_is_never_deleted(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)

    orchestrator.object_to_claim(task.task_id, claim_id=claim.claim_id, reviewer_id="reviewer_1", reason="concern A")
    orchestrator.object_to_claim(
        task.task_id, claim_id=claim.claim_id, reviewer_id="governance_admin", reason="concern B"
    )

    objections = orchestrator.repository.list_objections_for_claim(claim.claim_id)
    assert [o.reason for o in objections] == ["concern A", "concern B"]

    # Objecting to a claim that has since been superseded is still allowed
    # and preserved - explaining *why* it needed to be replaced.
    orchestrator.replace_claim(task.task_id, old_claim_id=claim.claim_id, claim_text="fixed", created_by="analyst_1")
    orchestrator.object_to_claim(
        task.task_id, claim_id=claim.claim_id, reviewer_id="reviewer_1", reason="concern C, after the fact"
    )
    objections_after = orchestrator.repository.list_objections_for_claim(claim.claim_id)
    assert len(objections_after) == 3


def test_object_to_claim_enforces_reviewer_separation(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)
    with pytest.raises(SelfApprovalError):
        orchestrator.object_to_claim(task.task_id, claim_id=claim.claim_id, reviewer_id="analyst_1", reason="x")


def test_object_to_claim_enforces_reviewer_authorization(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)
    with pytest.raises(UnauthorizedReviewerError):
        orchestrator.object_to_claim(task.task_id, claim_id=claim.claim_id, reviewer_id="random_person", reason="x")


# -- 5. effective claim-set computation -----------------------------------------


def test_compute_effective_claims_excludes_replaced_superseded_and_withdrawn():
    active = Claim(task_id="t", claim_text="a", model_id="m", created_by="c", lifecycle_status=ClaimLifecycleStatus.ACTIVE)
    replaced = Claim(task_id="t", claim_text="b", model_id="m", created_by="c", lifecycle_status=ClaimLifecycleStatus.REPLACED)
    superseded = Claim(
        task_id="t", claim_text="c", model_id="m", created_by="c", lifecycle_status=ClaimLifecycleStatus.SUPERSEDED
    )
    withdrawn = Claim(
        task_id="t", claim_text="d", model_id="m", created_by="c", lifecycle_status=ClaimLifecycleStatus.WITHDRAWN
    )

    effective = compute_effective_claims([active, replaced, superseded, withdrawn])

    assert effective == [active]


def test_effective_claim_set_reflects_a_live_replace_through_the_repository(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)
    new_claim = orchestrator.replace_claim(
        task.task_id, old_claim_id=claim.claim_id, claim_text="v2", created_by="analyst_1"
    )

    all_claims = orchestrator.repository.list_claims_for_task(task.task_id)
    assert {c.claim_id for c in all_claims} == {claim.claim_id, new_claim.claim_id}  # historical: both present

    effective = compute_effective_claims(all_claims)
    assert [c.claim_id for c in effective] == [new_claim.claim_id]  # effective: only the successor


# -- 6. claim_set_version increment ----------------------------------------------


def test_a_revision_that_changes_the_effective_claim_set_always_creates_a_new_version(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)
    assert task.claim_set_version == 1

    task_v2 = orchestrator.withdraw_claim(task.task_id, claim_id=claim.claim_id, reason="r")
    assert task_v2.claim_set_version == 2

    # A second, independent claim demonstrates version increments compound
    # (claim.claim_id is now WITHDRAWN/inactive, so a further revision on
    # *it* would raise - see test_withdraw_claim_rejects_an_already_inactive_claim -
    # this uses a fresh claim to show the version counter itself keeps
    # advancing across successive, unrelated revisions).
    other_claim = Claim(task_id=task.task_id, claim_text="second claim", model_id="m", created_by="analyst_1")
    orchestrator.repository.save_claim(other_claim)
    task_v3 = orchestrator.withdraw_claim(task.task_id, claim_id=other_claim.claim_id, reason="r2")
    assert task_v3.claim_set_version == 3


# -- 7/8/9. stale approval propagation + publication blocked + reapproval -------


def test_replace_claim_after_approval_makes_the_prior_approval_stale(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)
    task = _approve(orchestrator, task.task_id)
    assert task.state == TaskState.APPROVED

    orchestrator.replace_claim(task.task_id, old_claim_id=claim.claim_id, claim_text="corrected", created_by="analyst_1")

    result = evaluate_publication_gate(task.task_id, orchestrator.repository)
    assert not result.passed
    assert any("stale" in r for r in result.reasons)


def test_publication_is_blocked_while_approval_is_stale(orchestrator):
    task, _, claim = _run_to_awaiting_review(orchestrator)
    task = _approve(orchestrator, task.task_id)
    orchestrator.replace_claim(task.task_id, old_claim_id=claim.claim_id, claim_text="corrected", created_by="analyst_1")

    with pytest.raises(PublicationGateError):
        orchestrator.publish(task.task_id)

    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.APPROVED  # unchanged, never silently published


def test_reapproving_the_new_version_unblocks_publication(orchestrator):
    task, evidence, claim = _run_to_awaiting_review(orchestrator)
    task = _approve(orchestrator, task.task_id)
    orchestrator.replace_claim(
        task.task_id,
        old_claim_id=claim.claim_id,
        claim_text="corrected",
        created_by="analyst_1",
        evidence_links=[EvidenceLinkSpec(evidence.evidence_id, "increase materially", SupportContribution.SUPPORTS)],
    )

    with pytest.raises(PublicationGateError):
        orchestrator.publish(task.task_id)

    # The task's state is still APPROVED (replace_claim doesn't touch
    # task.state), so a second review decision can't go through the normal
    # AWAITING_REVIEW-only submit_review() path - this proves the *gate*
    # itself, not the state machine, is what re-requires approval, exactly
    # like test_phase5_review_workflow.py's stale-approval test proves the
    # gate directly rather than assuming a live path exists yet.
    from afra.domain.models import Review

    reloaded = orchestrator.repository.get_task(task.task_id)
    orchestrator.repository.save_review(
        Review(
            task_id=task.task_id,
            reviewer_id="reviewer_1",
            decision=ReviewDecision.APPROVE,
            claim_set_version=reloaded.claim_set_version,
        )
    )

    result = evaluate_publication_gate(task.task_id, orchestrator.repository)
    assert result.passed, result.reasons

    published = orchestrator.publish(task.task_id)
    assert published.state == TaskState.PUBLISHED


# -- 10. historical claim preservation -------------------------------------------


def test_historical_claims_remain_queryable_with_original_content_after_every_revision_kind(orchestrator):
    task, evidence, claim = _run_to_awaiting_review(orchestrator)
    original_text = claim.claim_text

    replaced_new = orchestrator.replace_claim(
        task.task_id, old_claim_id=claim.claim_id, claim_text="replacement text", created_by="analyst_1"
    )
    withdrawn_task = orchestrator.withdraw_claim(
        task.task_id, claim_id=replaced_new.claim_id, reason="also withdrawn for this test"
    )
    assert withdrawn_task.task_id == task.task_id

    all_claims = orchestrator.repository.list_claims_for_task(task.task_id)
    by_id = {c.claim_id: c for c in all_claims}

    # Every claim ever created for this task is still present and
    # unmutated in content - only lifecycle metadata changed.
    assert len(all_claims) == 2
    assert by_id[claim.claim_id].claim_text == original_text
    assert by_id[claim.claim_id].lifecycle_status == ClaimLifecycleStatus.REPLACED
    assert by_id[replaced_new.claim_id].claim_text == "replacement text"
    assert by_id[replaced_new.claim_id].lifecycle_status == ClaimLifecycleStatus.WITHDRAWN

    # Nothing is effective any more (the only claim was replaced, then its
    # successor was withdrawn) - a real, honestly-reported empty set.
    assert compute_effective_claims(all_claims) == []


# -- run_validation / compute_sufficiency scoping to effective claims -----------


def test_run_validation_only_validates_effective_claims_not_superseded_history(orchestrator):
    """End-to-end: a revision's re-synthesis validates only the new claim,
    not the old one it superseded, even though both rows exist."""
    task, evidence, old_claim = _run_to_awaiting_review(orchestrator)
    orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.REQUEST_REVISION)
    task = orchestrator.resume_from_revision(task.task_id, needs_new_evidence=False)

    new_claim = orchestrator.draft_claim(
        task.task_id,
        prompt="revised",
        evidence_links=[EvidenceLinkSpec(evidence.evidence_id, "increase materially", SupportContribution.SUPPORTS)],
        created_by="analyst_1",
    )
    orchestrator.supersede_claim(task.task_id, old_claim_id=old_claim.claim_id, new_claim_id=new_claim.claim_id)
    orchestrator.complete_synthesis(task.task_id)

    results = orchestrator.run_validation(task.task_id)
    assert [r.claim_id for r in results] == [new_claim.claim_id]
