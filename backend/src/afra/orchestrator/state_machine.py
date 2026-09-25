"""The transition table from /docs/STATE_MACHINE.md, encoded directly.

This is the single source of truth for which ResearchTask.state transitions
are legal. Any component that wants to move a task between states must go
through Orchestrator, which consults this table - see
docs/STATE_MACHINE.md#invariants: "No component other than the orchestrator
may change a task's state."
"""

from __future__ import annotations

from afra.domain.enums import TERMINAL_STATES, TaskState
from afra.domain.errors import IllegalTransitionError

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset({TaskState.PLANNING}),
    TaskState.PLANNING: frozenset(
        {TaskState.NEEDS_CLARIFICATION, TaskState.GATHERING_EVIDENCE}
    ),
    TaskState.NEEDS_CLARIFICATION: frozenset({TaskState.PLANNING}),
    TaskState.GATHERING_EVIDENCE: frozenset({TaskState.SYNTHESISING}),
    # SYNTHESISING -> SECURITY_BLOCKED is a Phase 4 addition, not in Phase
    # 0's original table (which only had VALIDATING -> SECURITY_BLOCKED) -
    # recorded as a deviation in docs/ROADMAP.md. Policy must block *before*
    # the model call that drafts a claim, which happens while the task is
    # still SYNTHESISING, not after it reaches VALIDATING.
    TaskState.SYNTHESISING: frozenset({TaskState.VALIDATING, TaskState.SECURITY_BLOCKED}),
    TaskState.VALIDATING: frozenset(
        {
            TaskState.INSUFFICIENT_EVIDENCE,
            TaskState.SECURITY_BLOCKED,
            TaskState.AWAITING_REVIEW,
        }
    ),
    TaskState.INSUFFICIENT_EVIDENCE: frozenset({TaskState.GATHERING_EVIDENCE}),
    TaskState.AWAITING_REVIEW: frozenset(
        # REJECTED is a Phase 5 addition, not in Phase 0's original table -
        # see docs/ROADMAP.md's Phase 5 deviation entry.
        {TaskState.APPROVED, TaskState.REVISION_REQUESTED, TaskState.REJECTED}
    ),
    TaskState.REVISION_REQUESTED: frozenset(
        {TaskState.GATHERING_EVIDENCE, TaskState.SYNTHESISING}
    ),
    TaskState.APPROVED: frozenset({TaskState.PUBLISHED}),
    # Terminal states: no outgoing transitions.
    TaskState.SECURITY_BLOCKED: frozenset(),
    TaskState.REJECTED: frozenset(),
    TaskState.PUBLISHED: frozenset(),
    TaskState.FAILED: frozenset(),
}


def is_legal_transition(from_state: TaskState, to_state: TaskState) -> bool:
    """FAILED is reachable from any non-terminal state (an operational
    failure can happen at any point) - docs/STATE_MACHINE.md transition
    table, last row. Every other transition must appear in
    ALLOWED_TRANSITIONS explicitly.
    """
    if to_state == TaskState.FAILED:
        return from_state not in TERMINAL_STATES
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def require_legal_transition(from_state: TaskState, to_state: TaskState) -> None:
    if not is_legal_transition(from_state, to_state):
        raise IllegalTransitionError(
            f"{from_state.value} -> {to_state.value} is not a legal transition "
            "per docs/STATE_MACHINE.md"
        )
