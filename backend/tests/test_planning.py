"""Direct tests of the planner, independent of the orchestrator."""

from __future__ import annotations

import pytest

from afra.domain.errors import PlanningError
from afra.domain.models import ResearchTask
from afra.planning.planner import decompose_question
from afra.providers.test_double import TestDoubleProvider


def _ensure_task(repository, task_id="task-1"):
    task = ResearchTask(question_text="q", created_by="analyst_1")
    task.task_id = task_id
    repository.save_task(task)
    return task_id


def test_decompose_question_with_two_companies_produces_2_to_4_subquestions(repository):
    task_id = _ensure_task(repository)
    subquestions = decompose_question(
        task_id,
        "Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI infrastructure risk.",
        ["Contoso Cloud Corp", "Fabrikam Systems Inc"],
        TestDoubleProvider(),
        repository,
    )
    assert 2 <= len(subquestions) <= 4
    assert any("Contoso Cloud Corp" in q for q in subquestions)
    assert any("Fabrikam Systems Inc" in q for q in subquestions)


def test_decompose_question_records_a_model_call(repository):
    task_id = _ensure_task(repository)

    decompose_question(
        task_id, "q", ["Contoso Cloud Corp", "Fabrikam Systems Inc"], TestDoubleProvider(), repository
    )

    calls = repository.list_model_calls_for_task(task_id)
    assert len(calls) == 1
    assert calls[0].purpose == "plan"


def test_decompose_question_with_zero_companies_fails_safely(repository):
    """The planner's safe-failure path: a question resolving to no known
    companies at all must not be silently guessed at - see
    docs/ROADMAP.md Phase 2 failure case "planner returns no usable
    subquestions -> safe failure". (Exactly one company is a valid case
    since Phase 3 - see test_decompose_question_with_one_company_succeeds.)
    """
    task_id = _ensure_task(repository)
    with pytest.raises(PlanningError):
        decompose_question(
            task_id,
            "Compare Nvidia and AMD's AI exposure.",
            [],  # no companies resolved - deliberately underspecified
            TestDoubleProvider(),
            repository,
        )


def test_decompose_question_with_one_company_succeeds(repository):
    """Phase 3 change (recorded in docs/ROADMAP.md): a single-company,
    non-comparison question is a legitimate bounded task once the
    interpreter can resolve scope to exactly one company - the planner
    must produce a usable (if smaller) subquestion set for it, not fail.
    Phase 2 never exercised this because it never had real interpretation;
    only the empty-companies case remains a safe-failure path (see
    test_decompose_question_with_fewer_than_two_companies_fails_safely for
    that renamed-in-spirit case with 0 companies).
    """
    task_id = _ensure_task(repository)
    subquestions = decompose_question(
        task_id,
        "What does Contoso disclose?",
        ["Contoso Cloud Corp"],
        TestDoubleProvider(),
        repository,
    )
    assert 2 <= len(subquestions) <= 4
    assert all("Contoso Cloud Corp" in q for q in subquestions)


def test_planning_failure_is_still_recorded_as_a_model_call(repository):
    """Even a failed planning attempt is part of the audit trace - a
    PlanningError must not suppress the ModelCall record."""
    task_id = _ensure_task(repository)

    with pytest.raises(PlanningError):
        decompose_question(task_id, "q", [], TestDoubleProvider(), repository)

    calls = repository.list_model_calls_for_task(task_id)
    assert len(calls) == 1
