"""GovernancePolicy - see docs/GOVERNANCE_POLICY.md.

Three narrow rule groups, each a direct, explicit encoding of a governance
concept this system already had (as hardcoded configuration, scattered
across afra.policy.routing_policy / afra.review.publication_gate /
afra.sufficiency.coverage) before this module existed - nothing here is a
new product behaviour or a general-purpose rule language:

- `data_class_policies` (one DataClassPolicy per Classification): which
  provider classes are allowed/blocked, whether a private provider is
  required, whether human approval is implied, and whether the
  classification blocks unconditionally regardless of DLP scan outcome -
  the exact shape afra.policy.routing_policy.ModelRoutingRule already had,
  now versioned and assignable per task instead of a single global table.
- `review_policy`: the Phase 5 review/publication rules (separation of
  duties, reviewer authorization is *not* policy data - see
  docs/GOVERNANCE_POLICY.md's "what remains deterministic code" section -
  version-integrity/staleness, conflict-abstention).
- `evidence_policy`: the Phase 3 sufficiency rules (minimum evidence per
  subquestion, conflicting evidence blocking sufficiency, unsupported
  claims blocking publication).

GovernancePolicy itself, and every nested rule dataclass, is frozen
(immutable) - see docs/GOVERNANCE_POLICY.md#versioning for what
"immutable by version" means in practice. Activation is a separate store
pointer, never a mutation of these objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
from types import MappingProxyType
from collections.abc import Mapping

from afra.domain.enums import Classification, PolicyStatus, ProviderClass
from afra.domain.errors import InvalidPolicyError



@dataclass(frozen=True)
class DataClassPolicy:
    """Governance rule for one Classification level - see this module's
    docstring. Mirrors afra.policy.routing_policy.ModelRoutingRule's shape,
    plus two fields that table never expressed as data:
    `blocked_provider_classes` (an explicit deny-list, checked against
    `allowed_provider_classes` for contradictions - see validate_policy())
    and `unconditional_block` (true for exactly the case
    afra.policy.enforcement's pre-existing RESTRICTED special case already
    hardcoded: block regardless of what a DLP scan finds).
    """

    classification: Classification
    allowed_provider_classes: frozenset[ProviderClass] = field(default_factory=frozenset)
    blocked_provider_classes: frozenset[ProviderClass] = field(default_factory=frozenset)
    require_private_provider: bool = False
    require_human_approval: bool = False
    unconditional_block: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_provider_classes", frozenset(self.allowed_provider_classes))
        object.__setattr__(self, "blocked_provider_classes", frozenset(self.blocked_provider_classes))

    def effective_allowed_provider_classes(self) -> frozenset[ProviderClass]:
        """allowed minus blocked, further narrowed to PRIVATE_LOCAL alone
        when require_private_provider is set - the single computation
        afra.policy.enforcement reads instead of duplicating this logic."""
        allowed = self.allowed_provider_classes - self.blocked_provider_classes
        if self.require_private_provider:
            allowed = allowed & frozenset({ProviderClass.PRIVATE_LOCAL})
        return allowed

    def to_dict(self) -> dict:
        return {
            "classification": self.classification.value,
            "allowed_provider_classes": sorted(c.value for c in self.allowed_provider_classes),
            "blocked_provider_classes": sorted(c.value for c in self.blocked_provider_classes),
            "require_private_provider": self.require_private_provider,
            "require_human_approval": self.require_human_approval,
            "unconditional_block": self.unconditional_block,
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(data: dict) -> "DataClassPolicy":
        _known_fields(data, DataClassPolicy)
        try:
            classification = Classification(data["classification"])
            allowed = frozenset(ProviderClass(c) for c in data.get("allowed_provider_classes", []))
            blocked = frozenset(ProviderClass(c) for c in data.get("blocked_provider_classes", []))
        except ValueError as exc:
            raise InvalidPolicyError(f"unknown data class or provider class in policy spec: {exc}") from exc
        except KeyError as exc:
            raise InvalidPolicyError(f"data class policy missing required field: {exc}") from exc
        return DataClassPolicy(
            classification=classification,
            allowed_provider_classes=allowed,
            blocked_provider_classes=blocked,
            require_private_provider=data.get("require_private_provider", False),
            require_human_approval=data.get("require_human_approval", False),
            unconditional_block=data.get("unconditional_block", False),
            notes=data.get("notes", ""),
        )


@dataclass(frozen=True)
class ReviewPolicy:
    """Review/publication governance rules - see this module's docstring.
    Every default here (True) matches this system's existing,
    pre-policy-layer, structurally-always-true behaviour exactly - see
    docs/GOVERNANCE_POLICY.md#what-remains-deterministic-code for which of
    these actually gate a conditional branch versus remain declarative
    because the underlying state machine has no "skip this" path at all.
    """

    human_review_required: bool = True
    separation_of_duties_required: bool = True
    publication_requires_current_version_approval: bool = True
    conflict_requires_abstention: bool = True
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "human_review_required": self.human_review_required,
            "separation_of_duties_required": self.separation_of_duties_required,
            "publication_requires_current_version_approval": self.publication_requires_current_version_approval,
            "conflict_requires_abstention": self.conflict_requires_abstention,
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(data: dict) -> "ReviewPolicy":
        _known_fields(data, ReviewPolicy)
        return ReviewPolicy(
            human_review_required=data.get("human_review_required", True),
            separation_of_duties_required=data.get("separation_of_duties_required", True),
            publication_requires_current_version_approval=data.get("publication_requires_current_version_approval", True),
            conflict_requires_abstention=data.get("conflict_requires_abstention", True),
            notes=data.get("notes", ""),
        )


@dataclass(frozen=True)
class EvidencePolicy:
    """Evidence-sufficiency governance rules - see this module's docstring.
    `minimum_evidence_per_subquestion=1` is not an invented threshold: it
    is this system's existing rule, made explicit - afra.sufficiency
    .coverage has always treated a subquestion with zero linked evidence as
    uncovered (`len(links) > 0`); this field generalises that exact check,
    default-equal to it; higher minima are explicit opt-in versions.
    """

    minimum_evidence_per_subquestion: int = 1
    conflicting_evidence_blocks_sufficiency: bool = True
    unsupported_claims_block_publication: bool = True
    insufficient_evidence_requires_abstention: bool = True
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "minimum_evidence_per_subquestion": self.minimum_evidence_per_subquestion,
            "conflicting_evidence_blocks_sufficiency": self.conflicting_evidence_blocks_sufficiency,
            "unsupported_claims_block_publication": self.unsupported_claims_block_publication,
            "insufficient_evidence_requires_abstention": self.insufficient_evidence_requires_abstention,
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(data: dict) -> "EvidencePolicy":
        _known_fields(data, EvidencePolicy)
        return EvidencePolicy(
            minimum_evidence_per_subquestion=data.get("minimum_evidence_per_subquestion", 1),
            conflicting_evidence_blocks_sufficiency=data.get("conflicting_evidence_blocks_sufficiency", True),
            unsupported_claims_block_publication=data.get("unsupported_claims_block_publication", True),
            insufficient_evidence_requires_abstention=data.get("insufficient_evidence_requires_abstention", True),
            notes=data.get("notes", ""),
        )


@dataclass(frozen=True)
class GovernancePolicy:
    """One immutable, versioned governance specification - see this
    module's docstring and docs/GOVERNANCE_POLICY.md.

    `(policy_id, version)` identifies immutable content, including status.
    The repository's separate active-policy pointer controls new task binding.
    """

    policy_id: str
    version: int
    name: str
    status: PolicyStatus
    description: str
    data_class_policies: Mapping[Classification, DataClassPolicy]
    review_policy: ReviewPolicy
    evidence_policy: EvidencePolicy
    created_at: datetime
    effective_from: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_class_policies", MappingProxyType(dict(self.data_class_policies)))

    def data_class_policy(self, classification: Classification) -> DataClassPolicy:
        """The DataClassPolicy for `classification` - raises
        InvalidPolicyError (not KeyError) if this policy is missing an
        entry for a real Classification value, which validate_policy()
        should already have caught before this policy was ever persisted
        or assigned to a task; this is a defensive re-check, not the
        primary validation path.
        """
        try:
            return self.data_class_policies[classification]
        except KeyError:
            raise InvalidPolicyError(
                f"policy {self.policy_id} v{self.version} has no rule for classification {classification.value}"
            ) from None

    def identity(self) -> tuple[str, int]:
        return (self.policy_id, self.version)

    def to_dict(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "name": self.name,
            "status": self.status.value,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "data_class_policies": {
                c.value: p.to_dict() for c, p in self.data_class_policies.items()
            },
            "review_policy": self.review_policy.to_dict(),
            "evidence_policy": self.evidence_policy.to_dict(),
        }

    @staticmethod
    def from_dict(data: dict) -> "GovernancePolicy":
        try:
            return GovernancePolicy._from_dict(data)
        except InvalidPolicyError:
            raise
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise InvalidPolicyError(f"malformed policy specification: {exc}") from exc

    @staticmethod
    def _from_dict(data: dict) -> "GovernancePolicy":
        _known_fields(data, GovernancePolicy)
        try:
            policy_id = data["policy_id"]
            version = data["version"]
            name = data["name"]
            status = PolicyStatus(data["status"])
            description = data["description"]
            raw_data_class_policies = data["data_class_policies"]
            raw_review_policy = data["review_policy"]
            raw_evidence_policy = data["evidence_policy"]
        except KeyError as exc:
            raise InvalidPolicyError(f"policy spec missing required field: {exc}") from exc
        except ValueError as exc:
            raise InvalidPolicyError(f"policy spec has an invalid value: {exc}") from exc

        data_class_policies: dict[Classification, DataClassPolicy] = {}
        for raw_classification, raw_policy in raw_data_class_policies.items():
            try:
                classification = Classification(raw_classification)
            except ValueError as exc:
                raise InvalidPolicyError(f"unknown data class in policy spec: {raw_classification!r}") from exc
            data_class_policies[classification] = DataClassPolicy.from_dict(raw_policy)

        created_at_raw = data["created_at"]
        effective_from_raw = data.get("effective_from")
        policy = GovernancePolicy(
            policy_id=policy_id,
            version=version,
            name=name,
            status=status,
            description=description,
            data_class_policies=data_class_policies,
            review_policy=ReviewPolicy.from_dict(raw_review_policy),
            evidence_policy=EvidencePolicy.from_dict(raw_evidence_policy),
            created_at=datetime.fromisoformat(created_at_raw) if created_at_raw else None,
            effective_from=datetime.fromisoformat(effective_from_raw) if effective_from_raw else None,
        )
        validate_policy_or_raise(policy)
        return policy


def _known_fields(data: dict, cls: type) -> None:
    if not isinstance(data, dict):
        raise InvalidPolicyError(f"{cls.__name__} must be an object")
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        raise InvalidPolicyError(f"unknown {cls.__name__} fields: {sorted(unknown)}")


def validate_policy(policy: GovernancePolicy) -> list[str]:
    """Validate types, contradictions and the narrow enforcement capabilities."""
    problems = []
    for name in ("policy_id", "name", "description"):
        value = getattr(policy, name)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{name} must be a nonempty string")
    if type(policy.version) is not int or policy.version < 1:
        problems.append("version must be a positive integer")
    if not isinstance(policy.status, PolicyStatus):
        problems.append("unknown policy status")
    for name in ("created_at", "effective_from"):
        value = getattr(policy, name)
        if value is None and name == "effective_from":
            continue
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            problems.append(f"{name} must be a timezone-aware datetime")
    if set(policy.data_class_policies) != set(Classification):
        problems.append("data_class_policies must contain exactly all known data classes")
    for classification, rule in policy.data_class_policies.items():
        if not isinstance(classification, Classification) or not isinstance(rule, DataClassPolicy):
            problems.append("unknown data class or invalid data-class rule")
            continue
        if not isinstance(rule.classification, Classification) or rule.classification != classification:
            problems.append("data-class rule does not match its key")
        for classes in (rule.allowed_provider_classes, rule.blocked_provider_classes):
            if any(not isinstance(c, ProviderClass) for c in classes):
                problems.append("unknown provider class")
        if rule.allowed_provider_classes & rule.blocked_provider_classes:
            problems.append("provider classes cannot be both allowed and blocked")
        if rule.require_private_provider and ProviderClass.PRIVATE_LOCAL not in (
            rule.allowed_provider_classes - rule.blocked_provider_classes
        ):
            problems.append("require_private_provider requires an allowed, unblocked PRIVATE_LOCAL")
        if rule.unconditional_block and rule.effective_allowed_provider_classes():
            problems.append("unconditional_block contradicts effective allowed providers")
        if not isinstance(rule.notes, str):
            problems.append("rule notes must be a string")
        for name in ("require_private_provider", "require_human_approval", "unconditional_block"):
            if type(getattr(rule, name)) is not bool:
                problems.append(f"{name} must be boolean")
    if not isinstance(policy.review_policy, ReviewPolicy) or not isinstance(policy.evidence_policy, EvidencePolicy):
        return problems + ["invalid review/evidence rule group"]
    for group in (policy.review_policy, policy.evidence_policy):
        if not isinstance(group.notes, str):
            problems.append("rule notes must be a string")
        for f in fields(group):
            if f.name not in ("notes", "minimum_evidence_per_subquestion") and type(getattr(group, f.name)) is not bool:
                problems.append(f"{f.name} must be boolean")
    minimum = policy.evidence_policy.minimum_evidence_per_subquestion
    if type(minimum) is not int or minimum < 1:
        problems.append("minimum_evidence_per_subquestion must be a positive integer")
    # No autonomous approval, stale approval, unsupported-publication, or
    # insufficient-evidence bypass is implemented by the existing workflow.
    for group, name in (
        (policy.review_policy, "human_review_required"),
        (policy.review_policy, "publication_requires_current_version_approval"),
        (policy.evidence_policy, "unsupported_claims_block_publication"),
        (policy.evidence_policy, "insufficient_evidence_requires_abstention"),
    ):
        if getattr(group, name) is not True:
            problems.append(f"{name}=False is unsupported by the current governance workflow")
    if policy.review_policy.conflict_requires_abstention != policy.evidence_policy.conflicting_evidence_blocks_sufficiency:
        problems.append("review/evidence conflict rules contradict each other")
    return problems


def validate_policy_or_raise(policy: GovernancePolicy) -> None:
    problems = validate_policy(policy)
    if problems:
        raise InvalidPolicyError("invalid governance policy: " + "; ".join(problems))
