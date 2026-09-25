"""interpret_question() - turns free text into a structured ResearchScope.

Grounded interpretation, not open-ended extraction: the interpreter only
ever resolves companies against the set actually available in this
system's document corpus (via the same search_documents() tool the rest of
the system uses), and axes against a small fixed domain vocabulary. It
cannot "recognise" a company or axis it has no data for - a question about
Nvidia and AMD is genuinely uninterpretable here, not because the model
doesn't know who they are, but because this system has no documents about
them. That's a deliberate property (interpretation is grounded in what the
system can act on, not what a general-purpose LLM happens to know), not a
limitation of the test double specifically - a real provider adapter later
would still need the known-company list to ground against.
"""

from __future__ import annotations

import re

from afra.domain.enums import Classification, ScopeFieldStatus
from afra.domain.models import ModelCall
from afra.interpretation.scope import ResearchScope, ScopeField
from afra.providers.base import ModelProvider
from afra.storage.repository import Repository
from afra.tools.fixtures import search_fixture_documents

KNOWN_COMPARISON_AXES = ("risk", "capital expenditure", "revenue")
COMPARISON_SIGNAL_WORDS = re.compile(r"\bcompar\w*\b|\bvs\b|\bversus\b", re.IGNORECASE)


def known_companies() -> list[str]:
    return sorted({doc.company for doc in search_fixture_documents()})


def build_interpretation_prompt(question_text: str) -> str:
    companies = ", ".join(known_companies())
    axes = ", ".join(KNOWN_COMPARISON_AXES)
    return (
        f"Research question: {question_text}\n"
        f"Known company vocabulary: {companies}\n"
        f"Known comparison axes: {axes}\n"
        "Task: identify which of the known companies (only from the known "
        "vocabulary) are mentioned or clearly implied, whether a "
        "comparison is being requested, and which of the known axes (if "
        "any) it targets. Respond in exactly this format:\n"
        "COMPANIES: <comma-separated known companies found, or NONE>\n"
        "COMPARISON_REQUESTED: <YES or NO>\n"
        "AXIS: <one of the known axes, or UNSPECIFIED>"
    )


def _parse_field(text: str, label: str) -> str:
    match = re.search(rf"{label}:\s*(.*)", text)
    return match.group(1).strip() if match else ""


def interpret_question(
    task_id: str, question_text: str, provider: ModelProvider, repository: Repository
) -> ResearchScope:
    prompt = build_interpretation_prompt(question_text)
    response = provider.complete(purpose="interpret", prompt=prompt)
    repository.save_model_call(
        ModelCall(
            task_id=task_id,
            purpose="interpret",
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

    companies_raw = _parse_field(response.text, "COMPANIES")
    comparison_requested = _parse_field(response.text, "COMPARISON_REQUESTED").upper() == "YES"
    axis_raw = _parse_field(response.text, "AXIS")

    found_companies = (
        [c.strip() for c in companies_raw.split(",") if c.strip()]
        if companies_raw and companies_raw.upper() != "NONE"
        else []
    )

    if found_companies:
        companies_field = ScopeField(ScopeFieldStatus.RESOLVED, value=found_companies)
    else:
        companies_field = ScopeField(
            ScopeFieldStatus.MISSING,
            reason=(
                "No company in this system's known corpus was named or "
                "clearly implied by the question."
            ),
        )

    if not comparison_requested:
        axis_field = ScopeField(ScopeFieldStatus.RESOLVED, value=None)
    elif axis_raw and axis_raw.upper() != "UNSPECIFIED":
        axis_field = ScopeField(ScopeFieldStatus.RESOLVED, value=axis_raw)
    else:
        axis_field = ScopeField(
            ScopeFieldStatus.AMBIGUOUS,
            reason=(
                "A comparison was requested but no specific axis (risk, "
                "capital expenditure, revenue) was specified."
            ),
        )

    return ResearchScope(companies=companies_field, comparison_axis=axis_field)
