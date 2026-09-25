"""Deterministic test-double model providers.

Used by tests and by the vertical slice in place of any real external LLM
adapter. They perform no network call. For most purposes (e.g.
draft_claim) they deterministically echo the prompt back, prefixed with a
purpose label.

For purpose="plan" and purpose="interpret" they do slightly more: extract
fixed-format lines from the (structurally fixed) prompts built by
afra.planning.planner.build_planning_prompt and
afra.interpretation.interpreter.build_interpretation_prompt, and
deterministically produce a structured response from them. Still not
"intelligence" - predictable text substitution against a fixed prompt
format.

Phase 4 addition: three provider classes exist now, not one -
TestDoubleProvider (EXTERNAL_STANDARD), EnterpriseApprovedTestDoubleProvider
(ENTERPRISE_APPROVED), and PrivateLocalTestDoubleProvider (PRIVATE_LOCAL) -
so afra.policy.enforcement has more than one real option to route between.
All three share the identical deterministic behaviour below
(_DeterministicTestDoubleBase); only their declared provider_class,
allowed_data_classes, and external_network_exposure differ, matching what a
real adapter of each kind would declare. Each instance also tracks how many
times complete() was actually invoked (`call_count`) and the purposes
passed (`calls`) - purely a test-observability aid, so tests can assert a
blocked/redirected call never reached a specific provider instance, not
just that the *audit trail* doesn't mention it.
"""

from __future__ import annotations

import re

from afra.domain.enums import Classification, ProviderClass
from afra.providers.base import ModelProvider, ModelResponse

_COMPANIES_LINE = re.compile(r"Known companies in scope:\s*(.*)")
_QUESTION_LINE = re.compile(r"Research question:\s*(.*)")
_VOCAB_LINE = re.compile(r"Known company vocabulary:\s*(.*)")
_AXES_LINE = re.compile(r"Known comparison axes:\s*(.*)")
_COMPARISON_SIGNAL = re.compile(r"\bcompar\w*\b|\bvs\b|\bversus\b", re.IGNORECASE)


class _DeterministicTestDoubleBase(ModelProvider):
    def __init__(self) -> None:
        self.call_count = 0
        self.calls: list[tuple[str, str]] = []  # (purpose, prompt) for every complete() call

    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        self.call_count += 1
        self.calls.append((purpose, prompt))
        if purpose == "plan":
            return self._plan(prompt)
        if purpose == "interpret":
            return self._interpret(prompt)
        return self._echo(purpose, prompt)

    def _echo(self, purpose: str, prompt: str) -> ModelResponse:
        words = prompt.split()
        text = f"[{purpose}] {prompt.strip()}"
        return ModelResponse(
            text=text, tokens_in=len(words), tokens_out=len(words), latency_ms=1, cost=0.0
        )

    def _plan(self, prompt: str) -> ModelResponse:
        tokens_in = len(prompt.split())
        match = _COMPANIES_LINE.search(prompt)
        companies: list[str] = []
        if match:
            raw = match.group(1).strip()
            if raw and raw.lower() != "none identified":
                companies = [c.strip() for c in raw.split(",") if c.strip()]

        if len(companies) == 0:
            # Deliberately unusable output: exercises the planner's
            # safe-failure path (see afra.planning.planner.decompose_question)
            # rather than guessing at a plan naming no known companies at all.
            return ModelResponse(text="", tokens_in=tokens_in, tokens_out=0, latency_ms=1, cost=0.0)

        if len(companies) == 1:
            # Phase 3 addition: a single-company, non-comparison question is
            # a legitimate bounded task - the interpreter can resolve scope
            # to exactly one company, and planning must handle that.
            company = companies[0]
            subquestions = [
                f"What does {company} disclose about AI infrastructure investment risk?",
                f"What evidence supports {company}'s stated AI infrastructure investment risk position?",
            ]
        else:
            first, second = companies[0], companies[1]
            subquestions = [
                f"What does {first} disclose about AI infrastructure investment risk?",
                f"What does {second} disclose about AI infrastructure investment risk?",
                f"How does {first}'s disclosure compare to {second}'s across its two most recent filings?",
            ]
        text = "\n".join(subquestions)
        return ModelResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=len(text.split()),
            latency_ms=1,
            cost=0.0,
        )

    def _interpret(self, prompt: str) -> ModelResponse:
        tokens_in = len(prompt.split())
        question_match = _QUESTION_LINE.search(prompt)
        vocab_match = _VOCAB_LINE.search(prompt)
        axes_match = _AXES_LINE.search(prompt)

        question = question_match.group(1).strip() if question_match else ""
        vocabulary = (
            [c.strip() for c in vocab_match.group(1).split(",") if c.strip()] if vocab_match else []
        )
        axes = [a.strip() for a in axes_match.group(1).split(",") if a.strip()] if axes_match else []

        found_companies = [c for c in vocabulary if c.lower() in question.lower()]
        comparison_requested = bool(_COMPARISON_SIGNAL.search(question))
        found_axis = next((a for a in axes if a.lower() in question.lower()), None)

        text = (
            f"COMPANIES: {', '.join(found_companies) if found_companies else 'NONE'}\n"
            f"COMPARISON_REQUESTED: {'YES' if comparison_requested else 'NO'}\n"
            f"AXIS: {found_axis if found_axis else 'UNSPECIFIED'}"
        )
        return ModelResponse(
            text=text, tokens_in=tokens_in, tokens_out=len(text.split()), latency_ms=1, cost=0.0
        )


class TestDoubleProvider(_DeterministicTestDoubleBase):
    """Stands in for a general-purpose external LLM API - the least
    trusted, most network-exposed provider class."""

    provider_id = "test-double-external"
    model_id = "deterministic-echo-v1"
    provider_class = ProviderClass.EXTERNAL_STANDARD
    allowed_data_classes = frozenset({Classification.PUBLIC})
    external_network_exposure = True


class EnterpriseApprovedTestDoubleProvider(_DeterministicTestDoubleBase):
    """Stands in for an enterprise-contracted endpoint with data-use
    guarantees - still network-exposed, but approved for INTERNAL data."""

    provider_id = "test-double-enterprise"
    model_id = "deterministic-echo-v1"
    provider_class = ProviderClass.ENTERPRISE_APPROVED
    allowed_data_classes = frozenset({Classification.PUBLIC, Classification.INTERNAL})
    external_network_exposure = True


class PrivateLocalTestDoubleProvider(_DeterministicTestDoubleBase):
    """Stands in for a self-hosted/private model - no external network
    exposure, approved up to CONFIDENTIAL. RESTRICTED is never approved for
    any provider class by default policy - see afra.policy.routing_policy.
    """

    provider_id = "test-double-private-local"
    model_id = "deterministic-echo-v1"
    provider_class = ProviderClass.PRIVATE_LOCAL
    allowed_data_classes = frozenset(
        {Classification.PUBLIC, Classification.INTERNAL, Classification.CONFIDENTIAL}
    )
    external_network_exposure = False
