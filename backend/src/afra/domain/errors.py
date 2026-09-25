from __future__ import annotations


class AfraError(Exception):
    """Base class for all domain errors in this package."""


class IllegalTransitionError(AfraError):
    """Raised when a state transition is attempted that the transition table
    in docs/STATE_MACHINE.md does not allow.
    """


class TaskNotFoundError(AfraError):
    pass


class SelfApprovalError(AfraError):
    """Raised when a reviewer attempts to approve their own task.

    See docs/CLAIM_EVIDENCE_MODEL.md#reviewer-separation. This is the
    concrete enforcement of that rule, not a UI convention.
    """


class PublicationGateError(AfraError):
    """Raised when APPROVED -> PUBLISHED is attempted but the publication
    gate in docs/CLAIM_EVIDENCE_MODEL.md#publication-gate does not pass.
    """


class PlanningError(AfraError):
    """Raised when the planner cannot produce a usable (2-4 item) set of
    subquestions from a research question. This is a safe-failure path,
    not a crash: the orchestrator catches this and transitions the task to
    FAILED rather than proceeding with zero or malformed subquestions.
    """


class SecurityPolicyError(AfraError):
    """Raised when a model call is blocked or requires approval under
    afra.policy - see docs/DATA_CLASSIFICATION.md. The orchestrator catches
    this and transitions the task to SECURITY_BLOCKED; the decision is
    always made by afra.policy.enforcement, never by the model itself - see
    docs/ROADMAP.md Phase 4 entry.
    """


class UnauthorizedReviewerError(AfraError):
    """Raised when submit_review() is called with a reviewer_id not in
    afra.identity.AUTHORIZED_REVIEWER_IDS, or resolve_security_event() is
    called with a resolved_by not in afra.identity.GOVERNANCE_REVIEWER_IDS.
    Phase 5 addition - a small, local, fixture-based allow-list, not an
    enterprise identity/access-control integration - see
    docs/CLAIM_EVIDENCE_MODEL.md#identity-scope-portfolio-implementation.
    """


class ClaimNotFoundError(AfraError):
    """Raised by a claim-revision action (replace_claim/supersede_claim/
    withdraw_claim/object_to_claim) when the given claim_id does not exist,
    or exists but belongs to a different task than the one specified - see
    docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics.
    """


class IllegalClaimRevisionError(AfraError):
    """Raised by a claim-revision action when the target claim is not
    currently ACTIVE (e.g. attempting to replace an already-withdrawn
    claim - a claim's terminal lifecycle_status is never re-opened), or
    when the owning task is in a terminal TaskState (a published, blocked,
    rejected, or failed task's claim set is no longer revisable) - see
    afra.domain.claim_lifecycle and
    docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics.
    """


class InvalidPolicyError(AfraError):
    """Raised by afra.governance.policy.validate_policy_or_raise() when a
    GovernancePolicy spec is structurally invalid - an unknown data class
    or provider class, a missing required version-metadata field, or an
    internally contradictory combination of rules (e.g. a provider class
    listed as both allowed and blocked for the same data class). Never
    raised for a business-rule disagreement ("RESTRICTED should always
    block") - only for specs a deterministic validator can prove are
    self-inconsistent. See docs/GOVERNANCE_POLICY.md.
    """


class PolicyNotFoundError(AfraError):
    """Raised when a (policy_id, version) pair does not exist in the
    governance-policy store."""


class PolicyVersionConflictError(AfraError):
    """Raised when attempting to save a GovernancePolicy whose
    (policy_id, version) already exists - policy versions are immutable
    once persisted (docs/GOVERNANCE_POLICY.md#versioning); a rule change
    must be a new version, never an overwrite of an existing one.
    """
