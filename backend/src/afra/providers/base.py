"""The model provider abstraction from /docs/ARCHITECTURE.md#model-provider-abstraction.

Design test (from that document): if a real adapter is deleted, everything
above this interface should still be meaningful and testable using only
test-double implementations. Every provider through Phase 4 is still a
deterministic test double - see docs/PHASE_4_REPORT.md.

Phase 4 addition: every provider declares provider_class,
allowed_data_classes, and external_network_exposure. These are read by
afra.policy.enforcement - a provider's own declared allowed_data_classes is
a *second*, independent check on top of the routing policy table (defence
in depth: even if the routing policy would permit a classification for a
provider's class in general, a specific provider instance can still refuse
to accept it). Nothing about this is inferred from the provider's runtime
behaviour or self-reported at call time - it is a static declaration
checked before any call is made.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from afra.domain.enums import Classification, ProviderClass


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    cost: float


class ModelProvider(ABC):
    """A provider adapter. Implemented: deterministic test doubles
    (afra.providers.test_double) and the Gemini / Antigravity-CLI
    claim-drafting adapters (afra.providers.real_model, purpose="draft_claim"
    only). OpenAI-compatible and self-hosted adapters remain future work;
    this interface is what they will implement.
    """

    provider_id: str
    model_id: str
    provider_class: ProviderClass
    allowed_data_classes: frozenset[Classification]
    external_network_exposure: bool

    @abstractmethod
    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        """Run one model call. `purpose` is a short label (e.g.
        "draft_claim") recorded on the resulting ModelCall audit row - see
        docs/CLAIM_EVIDENCE_MODEL.md#modelcall.

        Callers (afra.orchestrator) must never call this directly with
        classified content - afra.policy.enforcement.enforce_model_call_policy()
        must run first and select an eligible provider. This method itself
        does not and cannot check classification; it is the thing being
        protected, not the thing doing the protecting - see
        docs/PHASE_4_REPORT.md's "architecture discipline" section.
        """
