"""The publication gate from /docs/CLAIM_EVIDENCE_MODEL.md#publication-gate.

    final_status == APPROVED
    AND every claim in the memo's required-claims set has support_status
        satisfying the validation policy
    AND no unresolved SecurityEvent for this task has action_taken in
        {block, require_approval}
    AND the Review that set APPROVED satisfies reviewer separation

This is a pure function evaluated against persisted state, deliberately
separate from Orchestrator.publish() so it can be unit-tested directly
(docs/ROADMAP.md Phase 5 exit criteria: "unit-tested directly, not only via
end-to-end happy path" - applied here in Phase 1 for the slice that exists).

Phase 1 simplifications, recorded explicitly rather than silently assumed:
- There is no "memo" or "required-claims subset" concept yet; every Claim
  belonging to the task is treated as required.
- PARTIALLY_SUPPORTED is rejected outright in Phase 1. The documented
  "PARTIALLY_SUPPORTED only where the memo section explicitly allows and
  discloses it" exception depends on a memo/section concept that doesn't
  exist yet - so Phase 1 is conservative rather than guessing at that rule.

Phase 4 makes the SecurityEvent clause real: any persisted SecurityEvent for
this task with action_taken in {block, require_approval} and no
resolved_by fails the gate. In practice this clause is currently
unreachable via the one call site that produces SecurityEvents
(_draft_and_persist_claim already transitions the task to SECURITY_BLOCKED
and raises before APPROVED is ever reachable) - it exists here as the
documented, independently-checked defence-in-depth layer, not because a
live path through it has been observed. Phase 5 adds
afra.orchestrator.orchestrator.resolve_security_event() as the real
resolution workflow for require_approval events - but a resolved event
still cannot revive a task that is already SECURITY_BLOCKED (a terminal
state with no outgoing transitions - see docs/STATE_MACHINE.md), so this
clause remains unreachable via any live orchestrator path even in Phase 5;
see docs/PHASE_5_REPORT.md.

Phase 5 adds two more real, independently-checked clauses:
- **Reviewer authorization**: the Review that set APPROVED must have a
  reviewer_id in afra.identity.AUTHORIZED_REVIEWER_IDS - a small, local,
  fixture-based allow-list (not enterprise identity/access control - see
  afra.identity's module docstring), distinct from the pre-existing
  reviewer-separation check below.
- **Version integrity**: the Review that set APPROVED must have been
  recorded against the task's *current* claim_set_version. If the claim set
  changed since that approval (claim_set_version has since advanced), the
  approval is stale and does not authorize the current version - see
  docs/CLAIM_EVIDENCE_MODEL.md's version-integrity note. As with the
  SecurityEvent clause, there is currently no live orchestrator path that
  produces this (APPROVED only transitions to PUBLISHED, never back to
  SYNTHESISING - see docs/STATE_MACHINE.md), so this is proven directly
  against hand-constructed repository state, the same pattern already used
  for the other defence-in-depth clauses in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from afra.domain.claim_lifecycle import compute_effective_claims
from afra.domain.enums import DLPAction, ReviewDecision, SupportStatus, TaskState
from afra.identity import AUTHORIZED_REVIEWER_IDS
from afra.storage.repository import Repository


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    policy_id: str = "default"
    policy_version: int = 1


def evaluate_publication_gate(task_id: str, repository: Repository) -> GateResult:
    reasons: list[str] = []

    task = repository.get_task(task_id)
    policy = repository.policy_for_task(task_id)
    review_policy = policy.review_policy
    if task.state != TaskState.APPROVED:
        reasons.append(
            f"task.state is {task.state.value}, not APPROVED"
        )

    # Scoped to effective claims only (afra.domain.claim_lifecycle
    # .compute_effective_claims) - a claim already replaced/superseded/
    # withdrawn by a revision-semantics action is history, not part of what
    # publication requires SUPPORTED. Before any such action has ever been
    # used on a task, every claim is still ACTIVE, so this is identical to
    # requiring every claim - unchanged behaviour for every pre-existing
    # call path and every existing test.
    claims = compute_effective_claims(repository.list_claims_for_task(task_id))
    if not claims:
        reasons.append("task has no claims to publish")
    for claim in claims:
        unsupported_block = (claim.support_status == SupportStatus.UNSUPPORTED
                             and policy.evidence_policy.unsupported_claims_block_publication)
        other_invalid = claim.support_status not in (SupportStatus.SUPPORTED, SupportStatus.UNSUPPORTED)
        if unsupported_block or other_invalid:
            status = claim.support_status.value if claim.support_status else "UNVALIDATED"
            reasons.append(
                f"claim {claim.claim_id} has support_status={status}, "
                "required SUPPORTED (Phase 1 does not implement the "
                "disclosed-partial-support exception)"
            )

    blocking_actions = {DLPAction.BLOCK, DLPAction.REQUIRE_APPROVAL}
    for event in repository.list_security_events_for_task(task_id):
        if event.action_taken in blocking_actions and event.resolved_by is None:
            reasons.append(
                f"unresolved SecurityEvent {event.security_event_id} has "
                f"action_taken={event.action_taken.value} "
                "(docs/CLAIM_EVIDENCE_MODEL.md#publication-gate)"
            )

    reviews = [
        r
        for r in repository.list_reviews_for_task(task_id)
        if r.decision == ReviewDecision.APPROVE
    ]
    if review_policy.human_review_required and not reviews:
        reasons.append("no APPROVE review found for this task")
    elif reviews:
        latest_approval = max(reviews, key=lambda r: r.reviewed_at)
        if review_policy.separation_of_duties_required and latest_approval.reviewer_id == task.created_by:
            reasons.append(
                f"reviewer_id ({latest_approval.reviewer_id}) equals "
                f"created_by ({task.created_by}); self-approval is not "
                "permitted (docs/CLAIM_EVIDENCE_MODEL.md#reviewer-separation)"
            )
        if (latest_approval.policy_id, latest_approval.policy_version) != policy.identity():
            reasons.append("approval was recorded under a different governance policy version")
        if latest_approval.reviewer_id not in AUTHORIZED_REVIEWER_IDS:
            reasons.append(
                f"reviewer_id ({latest_approval.reviewer_id}) is not an "
                "authorized reviewer identity (afra.identity.AUTHORIZED_REVIEWER_IDS)"
            )
        if review_policy.publication_requires_current_version_approval and latest_approval.claim_set_version != task.claim_set_version:
            reasons.append(
                f"approval was recorded for claim_set_version="
                f"{latest_approval.claim_set_version}, but the task's current "
                f"claim_set_version is {task.claim_set_version} - the claim set "
                "changed since this approval, so it no longer authorizes the "
                "current version (stale approval)"
            )

    return GateResult(passed=not reasons, reasons=reasons, policy_id=policy.policy_id, policy_version=policy.version)
