"""Enumerations matching /docs/STATE_MACHINE.md and /docs/CLAIM_EVIDENCE_MODEL.md.

Names and values are kept identical to the documentation so the docs remain
the source of truth for what each value means.
"""

from __future__ import annotations

from enum import Enum


class TaskState(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    GATHERING_EVIDENCE = "GATHERING_EVIDENCE"
    SYNTHESISING = "SYNTHESISING"
    VALIDATING = "VALIDATING"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    SECURITY_BLOCKED = "SECURITY_BLOCKED"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    APPROVED = "APPROVED"
    # Phase 5 addition, not in Phase 0's original 13-state list - see
    # docs/ROADMAP.md's Phase 5 deviation entry. A reviewer's REJECT
    # decision (distinct from REQUEST_REVISION) needs its own terminal
    # outcome: a hard rejection on the merits, not a resumable revision
    # cycle. Mirrors SECURITY_BLOCKED's terminal philosophy for a
    # content/quality reason rather than a security one.
    REJECTED = "REJECTED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


TERMINAL_STATES = frozenset(
    {TaskState.SECURITY_BLOCKED, TaskState.REJECTED, TaskState.PUBLISHED, TaskState.FAILED}
)


class SupportStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SupportContribution(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    NEUTRAL = "neutral"


class ReviewDecision(str, Enum):
    APPROVE = "approve"
    REQUEST_REVISION = "request_revision"
    # Phase 5 addition: a hard rejection on the merits, distinct from
    # REQUEST_REVISION's resumable revision cycle - see TaskState.REJECTED.
    REJECT = "reject"


class Classification(str, Enum):
    """Data classification levels from /docs/DATA_CLASSIFICATION.md.

    Unenforced through Phase 3 - the field existed on Evidence/ModelCall so
    the data model matched the documented shape, but nothing acted on its
    value. Phase 4 (afra.policy) is where this becomes real, enforced
    routing/DLP policy.
    """

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


CLASSIFICATION_RANK: dict[Classification, int] = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.CONFIDENTIAL: 2,
    Classification.RESTRICTED: 3,
}


class ProviderClass(str, Enum):
    """Which class of model provider a ModelProvider adapter belongs to -
    see docs/DATA_CLASSIFICATION.md#model-routing-policy and
    afra.providers.base.ModelProvider. Every provider in Phase 4 is still a
    deterministic test double (see docs/PHASE_4_REPORT.md); these classes
    describe what a *real* adapter of that kind would declare.
    """

    EXTERNAL_STANDARD = "EXTERNAL_STANDARD"
    ENTERPRISE_APPROVED = "ENTERPRISE_APPROVED"
    PRIVATE_LOCAL = "PRIVATE_LOCAL"
    NO_MODEL = "NO_MODEL"


class DLPAction(str, Enum):
    """The five outcomes a policy/DLP decision can produce - see
    docs/DATA_CLASSIFICATION.md#dlp--security-policy-actions.
    """

    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"
    ROUTE_PRIVATE = "route_private"
    REQUIRE_APPROVAL = "require_approval"


class ClaimLifecycleStatus(str, Enum):
    """A claim's position in its own revision history - see
    docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics.

    ACTIVE is the only status a claim can be created with, and the only
    status counted in "the currently effective claim set"
    (afra.domain.claim_lifecycle.compute_effective_claims). The other three
    are terminal for that claim (nothing transitions a claim away from them
    again - a REPLACED claim is never un-replaced): a claim moves to
    exactly one of them, once, and the row is never deleted afterward -
    only ever queryable as history.
    """

    ACTIVE = "ACTIVE"
    # A direct, targeted substitution (afra.orchestrator.orchestrator
    # .replace_claim) - e.g. a reviewer-flagged factual correction, done
    # without a full re-synthesis pass.
    REPLACED = "REPLACED"
    # Superseded as part of a broader revision cycle's re-synthesis
    # (afra.orchestrator.orchestrator.supersede_claim) - the same relationship
    # as REPLACED (a successor claim exists), reached via a different path.
    SUPERSEDED = "SUPERSEDED"
    # Removed from the effective set with no successor at all (afra
    # .orchestrator.orchestrator.withdraw_claim) - e.g. evidence turned out
    # irrelevant, or the claim should never have been drafted.
    WITHDRAWN = "WITHDRAWN"


class PolicyStatus(str, Enum):
    """Immutable declaration at registration; activation is a separate pointer."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"


class ScopeFieldStatus(str, Enum):
    """Per-field classification for ResearchScope (Phase 3) - see
    docs/PHASE_3_REPORT.md and afra.interpretation.scope.
    """

    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    MISSING = "MISSING"
