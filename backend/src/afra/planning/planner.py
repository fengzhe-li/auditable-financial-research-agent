"""The minimal Research Planner from docs/ARCHITECTURE.md#research-planner,
narrowed to exactly what Phase 2 asks for: turn one bounded research
question into 2-4 structured subquestions.

Explicitly NOT built here (deferred, not silently skipped):

- Ambiguity detection. This planner does not decide whether the question
  needs clarification - Phase 1's request_clarification() is still called
  directly by the caller. A bounded, already-scoped question (this phase's
  target) doesn't exercise that path; a real ambiguity-detection algorithm
  deciding *when* to ask is future work.
- Autonomous tool selection. This planner returns subquestions as text. It
  does not decide which documents or tools answer each subquestion - the
  caller (in Phase 2, a test standing in for a future controller) still
  maps subquestions to retrieve_section() calls explicitly. Building that
  mapping automatically would be exactly the "complex agent framework"
  Phase 2 was told not to add - see the Phase 2 report's overengineering
  section.
- Replanning / iteration. One call, one shot. No loop that re-plans based
  on intermediate results.
"""

from __future__ import annotations

from afra.domain.enums import Classification
from afra.domain.errors import PlanningError
from afra.domain.models import ModelCall
from afra.providers.base import ModelProvider
from afra.storage.repository import Repository

MIN_SUBQUESTIONS = 2
MAX_SUBQUESTIONS = 4


def build_planning_prompt(question_text: str, known_companies: list[str]) -> str:
    companies_line = ", ".join(known_companies) if known_companies else "none identified"
    return (
        f"Research question: {question_text}\n"
        f"Known companies in scope: {companies_line}\n"
        "Task: propose 2-4 focused subquestions that would need to be "
        "answered with evidence to address the research question. One "
        "subquestion per line. Do not answer the subquestions."
    )


def _parse_subquestions(response_text: str) -> list[str]:
    lines = [line.strip(" \t-") for line in response_text.splitlines()]
    return [line for line in lines if line]


def decompose_question(
    task_id: str,
    question_text: str,
    known_companies: list[str],
    provider: ModelProvider,
    repository: Repository,
) -> list[str]:
    """Returns 2-4 subquestions, or raises PlanningError. Always records a
    ModelCall for the attempt, whether or not it succeeded - a failed
    planning attempt is still part of the audit trace.
    """
    prompt = build_planning_prompt(question_text, known_companies)
    response = provider.complete(purpose="plan", prompt=prompt)
    repository.save_model_call(
        ModelCall(
            task_id=task_id,
            purpose="plan",
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            routing_classification=Classification.PUBLIC,
            input_summary=prompt[:200],
            output_summary=response.text[:200],
            latency_ms=response.latency_ms,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost=response.cost,
        )
    )

    subquestions = _parse_subquestions(response.text)
    if not (MIN_SUBQUESTIONS <= len(subquestions) <= MAX_SUBQUESTIONS):
        raise PlanningError(
            f"planner produced {len(subquestions)} usable subquestion(s) for "
            f"task {task_id}; need {MIN_SUBQUESTIONS}-{MAX_SUBQUESTIONS}"
        )
    return subquestions
