"""Direct tests of the transition table against docs/STATE_MACHINE.md,
independent of the orchestrator or any persistence.
"""

from __future__ import annotations

import pytest

from afra.domain.enums import TERMINAL_STATES, TaskState
from afra.domain.errors import IllegalTransitionError
from afra.orchestrator.state_machine import is_legal_transition, require_legal_transition


def test_documented_happy_path_is_legal():
    path = [
        TaskState.CREATED,
        TaskState.PLANNING,
        TaskState.GATHERING_EVIDENCE,
        TaskState.SYNTHESISING,
        TaskState.VALIDATING,
        TaskState.AWAITING_REVIEW,
        TaskState.APPROVED,
        TaskState.PUBLISHED,
    ]
    for a, b in zip(path, path[1:]):
        assert is_legal_transition(a, b), f"{a} -> {b} should be legal"


def test_clarification_round_trip_is_legal():
    assert is_legal_transition(TaskState.PLANNING, TaskState.NEEDS_CLARIFICATION)
    assert is_legal_transition(TaskState.NEEDS_CLARIFICATION, TaskState.PLANNING)


def test_insufficient_evidence_only_resumes_via_gathering_evidence():
    assert is_legal_transition(
        TaskState.VALIDATING, TaskState.INSUFFICIENT_EVIDENCE
    )
    assert is_legal_transition(
        TaskState.INSUFFICIENT_EVIDENCE, TaskState.GATHERING_EVIDENCE
    )
    # No direct escape from INSUFFICIENT_EVIDENCE to review/approval/publish.
    assert not is_legal_transition(TaskState.INSUFFICIENT_EVIDENCE, TaskState.AWAITING_REVIEW)
    assert not is_legal_transition(TaskState.INSUFFICIENT_EVIDENCE, TaskState.APPROVED)
    assert not is_legal_transition(TaskState.INSUFFICIENT_EVIDENCE, TaskState.PUBLISHED)


def test_no_state_reaches_published_except_approved():
    for state in TaskState:
        if state == TaskState.APPROVED:
            assert is_legal_transition(state, TaskState.PUBLISHED)
        else:
            assert not is_legal_transition(state, TaskState.PUBLISHED), (
                f"{state} -> PUBLISHED must not be legal"
            )


def test_no_state_reaches_approved_except_awaiting_review():
    for state in TaskState:
        if state == TaskState.AWAITING_REVIEW:
            assert is_legal_transition(state, TaskState.APPROVED)
        else:
            assert not is_legal_transition(state, TaskState.APPROVED), (
                f"{state} -> APPROVED must not be legal"
            )


def test_security_blocked_is_terminal():
    for state in TaskState:
        assert not is_legal_transition(TaskState.SECURITY_BLOCKED, state)


def test_rejected_is_terminal():
    """Phase 5 addition - a reviewer's REJECT decision (distinct from
    REQUEST_REVISION) has no resumption path, mirroring SECURITY_BLOCKED's
    terminal philosophy for a content/quality reason rather than a security
    one - see docs/ROADMAP.md's Phase 5 deviation entry.
    """
    for state in TaskState:
        assert not is_legal_transition(TaskState.REJECTED, state)


def test_awaiting_review_can_reach_rejected():
    assert is_legal_transition(TaskState.AWAITING_REVIEW, TaskState.REJECTED)


def test_published_and_failed_are_terminal():
    for state in TaskState:
        assert not is_legal_transition(TaskState.PUBLISHED, state)
        assert not is_legal_transition(TaskState.FAILED, state)


def test_failed_reachable_from_any_non_terminal_state():
    """Uses TERMINAL_STATES directly (rather than a hand-maintained list)
    so this test can't silently drift out of sync with a newly added
    terminal state - see the Phase 5 REJECTED addition, which this test
    previously didn't know about.
    """
    non_terminal = [s for s in TaskState if s not in TERMINAL_STATES]
    for state in non_terminal:
        assert is_legal_transition(state, TaskState.FAILED)


def test_require_legal_transition_raises_on_illegal_move():
    with pytest.raises(IllegalTransitionError):
        require_legal_transition(TaskState.CREATED, TaskState.PUBLISHED)
