"""DEFAULT_GOVERNANCE_POLICY - the one canonical policy version (v1) that
preserves this system's pre-existing governance behaviour exactly. See
docs/GOVERNANCE_POLICY.md#the-default-policy.

The routing rules below are a fixed v1 snapshot of the existing routing
semantics. Future changes must create a new version; the legacy routing
table cannot silently redefine this version. Tests check their equivalence.

The review_policy/evidence_policy defaults below are not derived from a
single existing table the way routing is - they are the Phase 5/Phase 3
assumptions that were already true, unconditionally, in every prior phase's
code (separation of duties, current-version approval, conflict-blocks-
sufficiency, unsupported-blocks-publication) - see each field's docstring
in afra.governance.policy for exactly which prior hardcoded check it
formalises.

created_at is a fixed constant, not datetime.now() - this is a versioned,
immutable specification, not a live-stamped object; two different
processes importing this module must see byte-identical policy content,
including its own recorded creation time, not a value that drifts with
wall-clock import time.
"""

from __future__ import annotations

from datetime import datetime, timezone

from afra.domain.enums import Classification, PolicyStatus, ProviderClass
from afra.governance.policy import DataClassPolicy, EvidencePolicy, GovernancePolicy, ReviewPolicy, validate_policy_or_raise
from afra.policy.routing_policy import ModelRoutingRule

# Frozen v1 snapshot; edits to the legacy routing table must not redefine v1.
_V1_ROUTING = {
    Classification.PUBLIC: ModelRoutingRule(Classification.PUBLIC, frozenset({ProviderClass.EXTERNAL_STANDARD, ProviderClass.ENTERPRISE_APPROVED, ProviderClass.PRIVATE_LOCAL}), False, "An approved external LLM provider is allowed."),
    Classification.INTERNAL: ModelRoutingRule(Classification.INTERNAL, frozenset({ProviderClass.ENTERPRISE_APPROVED, ProviderClass.PRIVATE_LOCAL}), False, "Approved enterprise endpoint only - not a general public API."),
    Classification.CONFIDENTIAL: ModelRoutingRule(Classification.CONFIDENTIAL, frozenset({ProviderClass.PRIVATE_LOCAL}), False, "Private/self-hosted model only - not a default external provider."),
    Classification.RESTRICTED: ModelRoutingRule(Classification.RESTRICTED, frozenset(), True, "No LLM exposure without a separately recorded exception; no exception path is implemented."),
}

DEFAULT_POLICY_ID = "default"
DEFAULT_POLICY_VERSION = 1

# Fixed, not datetime.now() - see this module's docstring.
_DEFAULT_POLICY_CREATED_AT = datetime(2026, 9, 19, tzinfo=timezone.utc)


def _data_class_policy_from_routing_rule(classification: Classification) -> DataClassPolicy:
    rule = _V1_ROUTING[classification]
    return DataClassPolicy(
        classification=classification,
        allowed_provider_classes=rule.allowed_provider_classes,
        blocked_provider_classes=frozenset(),
        require_private_provider=(rule.allowed_provider_classes == frozenset({ProviderClass.PRIVATE_LOCAL})),
        require_human_approval=rule.requires_approval,
        # Matches afra.policy.enforcement's pre-existing hardcoded special
        # case exactly: a classification with no allowed provider classes
        # at all blocks unconditionally, regardless of DLP scan outcome -
        # true today only for RESTRICTED.
        unconditional_block=not rule.allowed_provider_classes,
        notes=rule.notes,
    )


DEFAULT_GOVERNANCE_POLICY = GovernancePolicy(
    policy_id=DEFAULT_POLICY_ID,
    version=DEFAULT_POLICY_VERSION,
    name="Default Governance Policy",
    status=PolicyStatus.ACTIVE,
    description=(
        "The system's original governance behaviour (Phases 4/5/3's data-classification/provider-routing, "
        "review/publication, and evidence-sufficiency rules), formalised as an explicit, versioned policy "
        "object rather than scattered hardcoded configuration. See docs/GOVERNANCE_POLICY.md."
    ),
    data_class_policies={
        classification: _data_class_policy_from_routing_rule(classification) for classification in Classification
    },
    review_policy=ReviewPolicy(
        human_review_required=True,
        separation_of_duties_required=True,
        publication_requires_current_version_approval=True,
        conflict_requires_abstention=True,
        notes=(
            "Every field here was already true, unconditionally, in this system's Phase 5 review workflow "
            "and Phase 3 sufficiency gate before this policy object existed - see "
            "docs/GOVERNANCE_POLICY.md#what-remains-deterministic-code."
        ),
    ),
    evidence_policy=EvidencePolicy(
        minimum_evidence_per_subquestion=1,
        conflicting_evidence_blocks_sufficiency=True,
        unsupported_claims_block_publication=True,
        notes=(
            "minimum_evidence_per_subquestion=1 is this system's existing rule (afra.sufficiency.coverage "
            "has always required len(links) > 0 for a subquestion to count as covered), not an invented "
            "threshold - see docs/GOVERNANCE_POLICY.md."
        ),
    ),
    created_at=_DEFAULT_POLICY_CREATED_AT,
    effective_from=_DEFAULT_POLICY_CREATED_AT,
)

# Validate the fixed canonical specification at import time.
validate_policy_or_raise(DEFAULT_GOVERNANCE_POLICY)
