"""Domain entities matching /docs/CLAIM_EVIDENCE_MODEL.md.

Field-for-field correspondence with that document is intentional. Anything
present in the doc but not needed to prove the Phase 1 vertical slice is
still represented here (so the shape matches later phases) but may be unused
until a later phase populates it - see /docs/ROADMAP.md Phase 1 entry for
what's actually exercised.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from afra.domain.enums import (
    ApprovalStatus,
    Classification,
    ClaimLifecycleStatus,
    DLPAction,
    ReviewDecision,
    SupportContribution,
    SupportStatus,
    TaskState,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ResearchTask:
    question_text: str
    created_by: str
    task_id: str = field(default_factory=lambda: _new_id("task"))
    resolved_scope: dict = field(default_factory=dict)
    state: TaskState = TaskState.CREATED
    classification: Classification = Classification.PUBLIC
    pending_clarification: str | None = None
    # Phase 5 addition: incremented by orchestrator.complete_synthesis()
    # every time a claim set is finalised for this task (so 1 after the
    # first synthesis, 2 after a revision cycle re-drafts, etc). A Review's
    # own claim_set_version records which version it actually reviewed -
    # see docs/CLAIM_EVIDENCE_MODEL.md#version-integrity-phase-5.
    claim_set_version: int = 0
    # Which governance policy version governs this task - see
    # docs/GOVERNANCE_POLICY.md. Defaults match
    # afra.governance.default_policy.DEFAULT_POLICY_ID/DEFAULT_POLICY_VERSION
    # exactly (kept as plain literals here, not an import, so
    # afra.domain stays free of a dependency on afra.governance - the two
    # are asserted equal by
    # tests/test_governance_policy.py::test_default_roundtrip_and_legacy_rules).
    # orchestrator.create_task() stamps these explicitly for every new
    # task; a task created before this field existed (or by any code that
    # never set it) is compatible by construction - it's simply assigned
    # to the same default policy every other pre-existing task effectively
    # already followed.
    policy_id: str = "default"
    policy_version: int = 1
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass
class Evidence:
    task_id: str
    source_document_id: str
    source_location: str
    raw_text: str
    evidence_id: str = field(default_factory=lambda: _new_id("evidence"))
    source_timestamp: datetime | None = None
    retrieved_at: datetime = field(default_factory=_now)
    retrieval_tool: str = "get_document"
    content_hash: str = ""
    classification: Classification = Classification.PUBLIC


@dataclass
class Claim:
    task_id: str
    claim_text: str
    model_id: str
    created_by: str
    claim_id: str = field(default_factory=lambda: _new_id("claim"))
    # Cached snapshot of the latest ValidationResult for this claim - see
    # docs/CLAIM_EVIDENCE_MODEL.md#validation-authority. Never set directly;
    # only written by Repository.record_validation_result().
    support_status: SupportStatus | None = None
    support_strength: float | None = None
    validator_result_id: str | None = None
    source_timestamp: datetime | None = None
    reviewer_id: str | None = None
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    # Phase 3 addition: which subquestion this claim was drafted to answer,
    # if any (draft_claim_from_evidence/draft_claim_without_evidence set
    # this; the general-purpose draft_claim() leaves it None). Used by
    # afra.sufficiency.coverage to check per-subquestion evidence coverage.
    subquestion: str | None = None
    # Revision-semantics fields - see
    # docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics and
    # afra.domain.claim_lifecycle. A claim is never deleted or overwritten
    # in place when revised; these fields record its place in its own
    # history instead.
    #
    # The claim_set_version this claim belongs to - stamped at draft time
    # as the version it is *pending* becoming part of (task.claim_set_version
    # + 1), matching complete_synthesis()'s pre-existing "+1 on completion"
    # semantics, so a claim's stamped version always equals
    # task.claim_set_version once the synthesis pass that drafted it
    # completes.
    claim_set_version: int = 0
    lifecycle_status: ClaimLifecycleStatus = ClaimLifecycleStatus.ACTIVE
    # Set on a *new* claim, pointing at the claim it replaces/supersedes.
    predecessor_claim_id: str | None = None
    # Set on the *old* claim once it has been replaced/superseded, pointing
    # at the claim that replaced/superseded it. Invariant this project
    # relies on (afra.domain.claim_lifecycle.compute_effective_claims):
    # lifecycle_status == ACTIVE implies successor_claim_id is None, and
    # vice versa - a claim is flipped away from ACTIVE in the same
    # operation that sets this field, never separately.
    successor_claim_id: str | None = None
    # Populated only when lifecycle_status == WITHDRAWN.
    withdrawn_reason: str | None = None


@dataclass
class ClaimEvidenceLink:
    """The authoritative claim<->evidence relationship.

    Claim.evidence_ids[] (if ever materialised as an in-memory convenience
    view) must be derived from these rows, never the other way around - see
    docs/CLAIM_EVIDENCE_MODEL.md#claimevidence_ids-vs-claimevidencelink.
    """

    claim_id: str
    evidence_id: str
    quote_span: str
    support_contribution: SupportContribution
    link_id: str = field(default_factory=lambda: _new_id("link"))
    relevance_score: float = 1.0


@dataclass
class ValidationResult:
    """The authoritative record of a support decision. See
    docs/CLAIM_EVIDENCE_MODEL.md#validation-authority.
    """

    claim_id: str
    computed_support_status: SupportStatus
    validation_id: str = field(default_factory=lambda: _new_id("validation"))
    computed_support_strength: float = 0.0
    citation_check_passed: bool = False
    conflicting_evidence_ids: list[str] = field(default_factory=list)
    missing_evidence_description: str | None = None
    validator_model_id: str = "deterministic-v1"
    validated_at: datetime = field(default_factory=_now)


@dataclass
class Review:
    """A persisted review decision. `comments` is Phase 1's original field
    name and is kept unchanged rather than renamed to the "rationale" name
    used in the Phase 5 spec, to avoid breaking every existing call site and
    test that already constructs a Review with `comments=` - see the Phase 5
    deviation entry in docs/ROADMAP.md. It serves the same role: the
    reviewer's stated reasoning for the decision.
    """

    task_id: str
    reviewer_id: str
    decision: ReviewDecision
    review_id: str = field(default_factory=lambda: _new_id("review"))
    comments: str = ""
    claims_reviewed: list[str] = field(default_factory=list)
    # Phase 5 additions:
    # - claim_set_version: the ResearchTask.claim_set_version this review
    #   decision actually evaluated, stamped at submission time. Lets the
    #   publication gate detect a stale approval (docs/ROADMAP.md Phase 5).
    # - security_context: a short, deterministic summary of this task's
    #   SecurityEvent history at review time, for the reviewer's/auditor's
    #   record - not itself a security decision.
    claim_set_version: int = 0
    security_context: str | None = None
    # Which governance policy version governed this review decision (e.g.
    # its separation-of-duties/reviewer-authorization/version-integrity
    # rules) - see docs/GOVERNANCE_POLICY.md#audit-trace. Same default-
    # literal rationale as ResearchTask.policy_id/policy_version above.
    policy_id: str = "default"
    policy_version: int = 1
    reviewed_at: datetime = field(default_factory=_now)


@dataclass
class ClaimObjection:
    """A reviewer's recorded objection to a specific claim - see
    docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics.

    Pure history: recording an objection never itself changes the claim's
    `lifecycle_status` or the task's `claim_set_version` - an objection is
    a flag/concern, not a revision. A claim can accumulate multiple
    objections over time (e.g. across several review cycles); none are
    ever deleted, so the full objection history for a claim is always
    queryable, whether or not it was ever acted on via
    afra.orchestrator.orchestrator.replace_claim/supersede_claim/
    withdraw_claim.
    """

    task_id: str
    claim_id: str
    reviewer_id: str
    reason: str
    objection_id: str = field(default_factory=lambda: _new_id("objection"))
    # The task's claim_set_version at the moment this objection was raised
    # - which version of the claim set the objection actually applies to,
    # mirroring Review.claim_set_version's role.
    claim_set_version: int = 0
    created_at: datetime = field(default_factory=_now)


@dataclass
class ModelCall:
    """Audit record of a model invocation. Written for every provider call,
    including test-double calls, from Phase 1 onward - see
    docs/THREAT_MODEL.md#audit-log-gaps.
    """

    task_id: str
    purpose: str
    provider_id: str
    model_id: str
    model_call_id: str = field(default_factory=lambda: _new_id("modelcall"))
    routing_classification: Classification = Classification.PUBLIC
    input_summary: str = ""
    output_summary: str = ""
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    created_at: datetime = field(default_factory=_now)


@dataclass
class ToolCall:
    """Audit record of a controlled-tool-layer invocation (search_documents,
    get_document, retrieve_section). Phase 2 addition - not in the original
    Phase 0 CLAIM_EVIDENCE_MODEL.md entity list, added because
    docs/ROADMAP.md Phase 2 explicitly requires tool calls to be part of the
    persisted trace, following the same pattern as ModelCall.
    """

    task_id: str
    tool_name: str
    tool_call_id: str = field(default_factory=lambda: _new_id("toolcall"))
    input_summary: str = ""
    output_summary: str = ""
    succeeded: bool = True
    created_at: datetime = field(default_factory=_now)


@dataclass
class StateTransition:
    """Audit record of a single ResearchTask state change. Phase 2 addition,
    same rationale as ToolCall: docs/ROADMAP.md Phase 2 requires state
    transitions to be part of the persisted trace, not just the task's
    current state column.
    """

    task_id: str
    from_state: TaskState
    to_state: TaskState
    transition_id: str = field(default_factory=lambda: _new_id("transition"))
    occurred_at: datetime = field(default_factory=_now)


@dataclass
class SecurityEvent:
    """Audit record of a policy/DLP decision - see
    docs/CLAIM_EVIDENCE_MODEL.md#securityevent (Phase 0's original entity
    definition) and docs/DATA_CLASSIFICATION.md. Phase 4 is the first phase
    that actually writes these; the entity itself was already documented in
    Phase 0.

    One SecurityEvent is persisted for every model-call policy decision,
    not only blocks - see docs/PHASE_4_REPORT.md for why (auditability of
    "nothing happened" is itself part of the trace, matching
    docs/THREAT_MODEL.md#audit-log-gaps).
    """

    task_id: str
    trigger: str  # what was being checked, e.g. "draft_claim:model_routing"
    classification: Classification
    action_taken: DLPAction
    security_event_id: str = field(default_factory=lambda: _new_id("secevent"))
    detected_categories: list[str] = field(default_factory=list)
    requested_provider_id: str | None = None
    selected_provider_id: str | None = None
    policy_result: str = ""
    policy_id: str = "default"
    policy_version: int = 1
    resolved_by: str | None = None
    created_at: datetime = field(default_factory=_now)
