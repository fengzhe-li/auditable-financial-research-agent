"""Claim revision semantics - see
docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics.

Deliberately a pure, read-only function module (the same "no service class,
just a function over persisted state" pattern already used by
afra.sufficiency.coverage and afra.review.publication_gate) - the actual
state changes (replace/supersede/withdraw/object) are orchestrator methods
(afra.orchestrator.orchestrator), because they need to mutate persisted
state and enforce task-state/authorization rules; this module only computes
a view over whatever the orchestrator already persisted.

Four terms this codebase uses precisely and does not conflate:

- **Historical claims**: every `Claim` row ever persisted for a task
  (`Repository.list_claims_for_task()`), regardless of `lifecycle_status`.
  Never deleted, always queryable - a full audit trail of what was drafted,
  corrected, and withdrawn.
- **Effective claims**: the subset of historical claims currently "in
  force" (`compute_effective_claims()` below) - what a reviewer approves
  and what the publication gate checks, not the full history.
- **Approved version**: the `claim_set_version` recorded on the `Review`
  that most recently set `APPROVED` (`Review.claim_set_version`) - which
  version of the effective claim set a human actually signed off on.
- **Current version**: `ResearchTask.claim_set_version` right now - the
  version the effective claim set is actually at. The publication gate
  (afra.review.publication_gate) requires approved version == current
  version; a mismatch is a stale approval.
"""

from __future__ import annotations

from afra.domain.enums import ClaimLifecycleStatus
from afra.domain.models import Claim


def compute_effective_claims(claims: list[Claim]) -> list[Claim]:
    """The currently effective claim set: every claim with
    lifecycle_status == ACTIVE, in the same order they were given.

    This is intentionally a flat filter, not a graph walk over
    predecessor/successor chains - it relies on an invariant every
    lifecycle-mutating orchestrator method (replace_claim, supersede_claim,
    withdraw_claim) maintains: a claim is flipped away from ACTIVE in the
    exact same operation that gives it a successor (or a withdrawn_reason),
    never separately and never left dangling. So at any point in time,
    ACTIVE claims are already exactly "not withdrawn, and not superseded by
    an active successor" - a REPLACED/SUPERSEDED claim's successor is
    either itself ACTIVE or has, in turn, already been replaced/superseded
    (in which case *its* successor is the one that's ACTIVE) - there is
    never a case where filtering on lifecycle_status alone gives a
    different answer than walking the chain to find each lineage's current
    tip.
    """
    return [claim for claim in claims if claim.lifecycle_status == ClaimLifecycleStatus.ACTIVE]
