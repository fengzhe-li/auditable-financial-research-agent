"""enforce_model_call_policy() - the single function that decides whether,
and how, a proposed model call may proceed, given the classification of the
content involved and the sensitive-content categories a deterministic DLP
scan detects in it.

Called by the orchestrator (afra.orchestrator.orchestrator._draft_and_persist_claim)
*before* any model call is made - not after, and not only at validation time.
See docs/DATA_CLASSIFICATION.md#where-this-runs-in-the-pipeline: "The
classification/routing check runs before any model call that would process
the data in question - not as a post-hoc audit."

This function is pure: it makes no network call, writes no persisted state,
and mutates nothing. The orchestrator is responsible for acting on the
returned EnforcementDecision (selecting a provider, sending the possibly
redacted prompt, raising SecurityPolicyError, and persisting a SecurityEvent
- persistence is deliberately kept out of this module so the decision logic
stays independently, deterministically testable with no repository).

Combines two independent checks (defence in depth, per
docs/DATA_CLASSIFICATION.md and afra.providers.base's module docstring):
1. The assigned governance policy's data-class rule
   (afra.governance.policy.DataClassPolicy, defaulting to
   afra.governance.default_policy.DEFAULT_GOVERNANCE_POLICY when the caller
   doesn't supply one - see docs/GOVERNANCE_POLICY.md): which
   ProviderClass values are permitted for this classification at all, and
   whether this classification blocks unconditionally regardless of DLP
   scan outcome.
2. The specific candidate provider's own declared `allowed_data_classes`:
   even a provider whose *class* is generally permitted can refuse a
   classification it does not itself declare support for.

This function's own decision *logic* (the branch structure below) is
unchanged from before the governance-policy layer existed - only where the
allowed-provider-classes/unconditional-block facts come from changed, from
a single hardcoded table (afra.policy.routing_policy.ROUTING_POLICY,
whose original semantics are frozen in default v1 - see
afra.governance.default_policy) to whichever GovernancePolicy version is
in effect for this call. Called with no `policy` argument, this function's
behaviour is byte-for-byte identical to before.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from afra.domain.enums import Classification, DLPAction, ProviderClass
from afra.governance.default_policy import DEFAULT_GOVERNANCE_POLICY
from afra.governance.policy import DataClassPolicy, GovernancePolicy, validate_policy_or_raise
from afra.policy.dlp import SENSITIVE_CATEGORIES, redact, scan_for_sensitive_content
from afra.providers.base import ModelProvider

_FALLBACK_ORDER = (ProviderClass.ENTERPRISE_APPROVED, ProviderClass.PRIVATE_LOCAL)


@dataclass(frozen=True)
class EnforcementDecision:
    action: DLPAction
    allowed: bool
    selected_provider_class: ProviderClass | None
    prompt: str
    detected_categories: list[str] = field(default_factory=list)
    reason: str = ""
    # Which governance policy version actually governed this decision -
    # for the audit trail (afra.orchestrator.orchestrator stamps this onto
    # the persisted SecurityEvent) - see
    # docs/GOVERNANCE_POLICY.md#audit-trace. Always populated: never None,
    # since enforce_model_call_policy() always resolves to some policy
    # (the caller's, or DEFAULT_GOVERNANCE_POLICY).
    policy_id: str = ""
    policy_version: int = 0


def _eligible(
    provider_class: ProviderClass,
    data_class_policy: DataClassPolicy,
    providers: dict[ProviderClass, ModelProvider],
) -> bool:
    if provider_class not in data_class_policy.effective_allowed_provider_classes():
        return False
    provider = providers.get(provider_class)
    return provider is not None and data_class_policy.classification in provider.allowed_data_classes


def enforce_model_call_policy(
    *,
    classification: Classification,
    prompt: str,
    requested_provider_class: ProviderClass,
    providers: dict[ProviderClass, ModelProvider],
    policy: GovernancePolicy | None = None,
) -> EnforcementDecision:
    policy = policy or DEFAULT_GOVERNANCE_POLICY
    validate_policy_or_raise(policy)
    data_class_policy = policy.data_class_policy(classification)
    detected = scan_for_sensitive_content(prompt)
    sensitive = [c for c in detected if c in SENSITIVE_CATEGORIES]

    # unconditional_block: no default routing exists for this
    # classification under this policy, full stop - see
    # docs/DATA_CLASSIFICATION.md's model-routing-policy table (RESTRICTED
    # is the one classification the default policy sets this for). This
    # check happens before any DLP consideration because an unconditional
    # block applies regardless of what the content actually contains.
    if data_class_policy.unconditional_block:
        return EnforcementDecision(
            action=DLPAction.BLOCK,
            allowed=False,
            selected_provider_class=None,
            prompt=prompt,
            detected_categories=detected,
            reason=(
                f"{classification.value} classification has no default model routing under policy "
                f"{policy.policy_id} v{policy.version} (docs/DATA_CLASSIFICATION.md#model-routing-policy); "
                "no policy exception is recorded for this task."
            ),
            policy_id=policy.policy_id,
            policy_version=policy.version,
        )

    if data_class_policy.require_human_approval:
        return EnforcementDecision(
            action=DLPAction.REQUIRE_APPROVAL, allowed=False,
            selected_provider_class=None, prompt=prompt, detected_categories=detected,
            reason="Assigned policy requires human approval before model exposure; no model-call exception path is implemented.",
            policy_id=policy.policy_id, policy_version=policy.version,
        )

    if sensitive:
        requested_eligible = _eligible(requested_provider_class, data_class_policy, providers)
        if requested_eligible and all(c in SENSITIVE_CATEGORIES for c in sensitive):
            redacted_prompt = redact(prompt, sensitive)
            return EnforcementDecision(
                action=DLPAction.REDACT,
                allowed=True,
                selected_provider_class=requested_provider_class,
                prompt=redacted_prompt,
                detected_categories=detected,
                reason=f"Sensitive content {sensitive} redacted before sending to {requested_provider_class.value}.",
                policy_id=policy.policy_id,
                policy_version=policy.version,
            )
        if _eligible(ProviderClass.PRIVATE_LOCAL, data_class_policy, providers):
            return EnforcementDecision(
                action=DLPAction.ROUTE_PRIVATE,
                allowed=True,
                selected_provider_class=ProviderClass.PRIVATE_LOCAL,
                prompt=prompt,
                detected_categories=detected,
                reason=(
                    f"Sensitive content {sensitive} detected; routed to "
                    "PRIVATE_LOCAL regardless of the originally requested provider."
                ),
                policy_id=policy.policy_id,
                policy_version=policy.version,
            )
        return EnforcementDecision(
            action=DLPAction.REQUIRE_APPROVAL,
            allowed=False,
            selected_provider_class=None,
            prompt=prompt,
            detected_categories=detected,
            reason=(
                f"Sensitive content {sensitive} detected and no eligible provider is "
                "registered for this classification. NOTE: no approval-resolution "
                "workflow exists yet (deferred to a later phase), so this currently "
                "has the same task-level consequence as `block` - see docs/PHASE_4_REPORT.md."
            ),
            policy_id=policy.policy_id,
            policy_version=policy.version,
        )

    if _eligible(requested_provider_class, data_class_policy, providers):
        return EnforcementDecision(
            action=DLPAction.ALLOW,
            allowed=True,
            selected_provider_class=requested_provider_class,
            prompt=prompt,
            detected_categories=detected,
            reason="Requested provider is within routing policy and declares this classification allowed.",
            policy_id=policy.policy_id,
            policy_version=policy.version,
        )

    for fallback in _FALLBACK_ORDER:
        if _eligible(fallback, data_class_policy, providers):
            return EnforcementDecision(
                action=DLPAction.ALLOW,
                allowed=True,
                selected_provider_class=fallback,
                prompt=prompt,
                detected_categories=detected,
                reason=(
                    f"Requested provider ({requested_provider_class.value}) is not eligible "
                    f"for {classification.value}; routed to {fallback.value} instead."
                ),
                policy_id=policy.policy_id,
                policy_version=policy.version,
            )

    return EnforcementDecision(
        action=DLPAction.BLOCK,
        allowed=False,
        selected_provider_class=None,
        prompt=prompt,
        detected_categories=detected,
        reason=f"No registered, eligible provider exists for {classification.value} content.",
        policy_id=policy.policy_id,
        policy_version=policy.version,
    )
