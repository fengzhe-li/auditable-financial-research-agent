"""Direct tests of interpret_question() and materially_unresolved_fields(),
independent of the orchestrator.
"""

from __future__ import annotations

from afra.domain.enums import ScopeFieldStatus
from afra.domain.models import ResearchTask
from afra.interpretation.interpreter import interpret_question
from afra.interpretation.scope import materially_unresolved_fields
from afra.providers.test_double import TestDoubleProvider


def _ensure_task(repository, task_id="task-1"):
    task = ResearchTask(question_text="q", created_by="analyst_1")
    task.task_id = task_id
    repository.save_task(task)
    return task_id


def test_comparison_with_known_companies_and_axis_is_fully_resolved(repository):
    task_id = _ensure_task(repository)
    scope = interpret_question(
        task_id,
        "Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI infrastructure risk.",
        TestDoubleProvider(),
        repository,
    )
    assert scope.companies.status == ScopeFieldStatus.RESOLVED
    assert set(scope.companies.value) == {"Contoso Cloud Corp", "Fabrikam Systems Inc"}
    assert scope.comparison_axis.status == ScopeFieldStatus.RESOLVED
    assert scope.comparison_axis.value == "risk"
    assert materially_unresolved_fields(scope) == []


def test_comparison_with_known_companies_but_no_axis_is_ambiguous(repository):
    """The canonical ambiguous case: 'AI exposure' names companies but not
    a specific comparable dimension."""
    task_id = _ensure_task(repository)
    scope = interpret_question(
        task_id,
        "Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI exposure.",
        TestDoubleProvider(),
        repository,
    )
    assert scope.companies.status == ScopeFieldStatus.RESOLVED
    assert scope.comparison_axis.status == ScopeFieldStatus.AMBIGUOUS
    assert scope.comparison_axis.reason

    unresolved = materially_unresolved_fields(scope)
    assert [name for name, _ in unresolved] == ["comparison_axis"]


def test_unknown_companies_are_missing_not_guessed(repository):
    """Nvidia and AMD are real named entities but not in this system's
    known corpus - the interpreter must not silently substitute or guess
    at companies it has no data for."""
    task_id = _ensure_task(repository)
    scope = interpret_question(
        task_id, "Compare Nvidia and AMD's AI exposure.", TestDoubleProvider(), repository
    )
    assert scope.companies.status == ScopeFieldStatus.MISSING
    assert scope.companies.value is None
    assert "known corpus" in scope.companies.reason or "known" in scope.companies.reason

    # "AI exposure" also names no known axis, and a comparison was
    # requested ("Compare..."), so comparison_axis is unresolved too -
    # both fields genuinely need clarification here, not just companies.
    unresolved = materially_unresolved_fields(scope)
    assert {name for name, _ in unresolved} == {"companies", "comparison_axis"}


def test_single_company_non_comparison_question_is_fully_resolved(repository):
    task_id = _ensure_task(repository)
    scope = interpret_question(
        task_id,
        "What does Contoso Cloud Corp disclose about AI infrastructure risk?",
        TestDoubleProvider(),
        repository,
    )
    assert scope.companies.status == ScopeFieldStatus.RESOLVED
    assert scope.companies.value == ["Contoso Cloud Corp"]
    # No comparison requested, so comparison_axis is RESOLVED (to "no axis
    # needed"), not ambiguous - see afra.interpretation.scope docstring.
    assert scope.comparison_axis.status == ScopeFieldStatus.RESOLVED
    assert materially_unresolved_fields(scope) == []


def test_interpretation_records_a_model_call(repository):
    task_id = _ensure_task(repository)
    interpret_question(
        task_id, "Compare Contoso Cloud Corp and Fabrikam Systems Inc's risk.", TestDoubleProvider(), repository
    )
    calls = repository.list_model_calls_for_task(task_id)
    assert len(calls) == 1
    assert calls[0].purpose == "interpret"


def test_defaulted_fields_are_always_resolved(repository):
    """time_range, document_types, source_constraints, source_scope are
    intentionally always RESOLVED-by-default in Phase 3 - see
    afra.interpretation.scope module docstring for why."""
    task_id = _ensure_task(repository)
    scope = interpret_question(
        task_id, "What does Contoso Cloud Corp disclose?", TestDoubleProvider(), repository
    )
    assert scope.time_range.status == ScopeFieldStatus.RESOLVED
    assert scope.document_types.status == ScopeFieldStatus.RESOLVED
    assert scope.source_constraints.status == ScopeFieldStatus.RESOLVED
    assert scope.source_scope.status == ScopeFieldStatus.RESOLVED
