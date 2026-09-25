"""The ModelRoutingRule table from docs/DATA_CLASSIFICATION.md#model-routing-policy,
encoded as data rather than inline conditionals - matching that document's
explicit instruction that this "is the default policy, not a hardcoded
`if classification == X` scattered through the codebase."

afra.policy.enforcement loads the rule for a proposed call's classification
and checks it against the requested/candidate provider's declared
provider_class. RESTRICTED maps to an empty allowed set: "No LLM exposure at
all, unless an explicit, separately-recorded policy exception allows a
specific narrow case" - no such exception is recorded anywhere in this
codebase, so RESTRICTED is unconditionally blocked in Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass

from afra.domain.enums import Classification, ProviderClass


@dataclass(frozen=True)
class ModelRoutingRule:
    classification: Classification
    allowed_provider_classes: frozenset[ProviderClass]
    requires_approval: bool
    notes: str


ROUTING_POLICY: dict[Classification, ModelRoutingRule] = {
    Classification.PUBLIC: ModelRoutingRule(
        classification=Classification.PUBLIC,
        allowed_provider_classes=frozenset(
            {ProviderClass.EXTERNAL_STANDARD, ProviderClass.ENTERPRISE_APPROVED, ProviderClass.PRIVATE_LOCAL}
        ),
        requires_approval=False,
        notes="An approved external LLM provider is allowed.",
    ),
    Classification.INTERNAL: ModelRoutingRule(
        classification=Classification.INTERNAL,
        allowed_provider_classes=frozenset(
            {ProviderClass.ENTERPRISE_APPROVED, ProviderClass.PRIVATE_LOCAL}
        ),
        requires_approval=False,
        notes="Approved enterprise endpoint only - not a general public API.",
    ),
    Classification.CONFIDENTIAL: ModelRoutingRule(
        classification=Classification.CONFIDENTIAL,
        allowed_provider_classes=frozenset({ProviderClass.PRIVATE_LOCAL}),
        requires_approval=False,
        notes="Private/self-hosted model only - not a default external provider.",
    ),
    Classification.RESTRICTED: ModelRoutingRule(
        classification=Classification.RESTRICTED,
        allowed_provider_classes=frozenset(),
        requires_approval=True,
        notes=(
            "No LLM exposure at all absent an explicit, separately-recorded "
            "policy exception - none exists in this codebase, so this rule "
            "always blocks in Phase 4 rather than actually offering an "
            "approval path (no approval-resolution workflow exists yet)."
        ),
    ),
}


def allowed_provider_classes_for(classification: Classification) -> frozenset[ProviderClass]:
    return ROUTING_POLICY[classification].allowed_provider_classes
