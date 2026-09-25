"""Runs three Phase 3 scenarios against throwaway SQLite files and prints
each one's trace: a successful autonomous run, a clarification path, and
an abstention (INSUFFICIENT_EVIDENCE) path. Used to generate the examples
in docs/PHASE_3_REPORT.md - not part of the test suite.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from afra.domain.enums import ReviewDecision
from afra.orchestrator.orchestrator import ResearchTaskOrchestrator
from afra.providers.test_double import TestDoubleProvider
from afra.storage.repository import Repository
from afra.trace import assemble_trace


def new_orchestrator(tmp_dir: str, name: str) -> ResearchTaskOrchestrator:
    db_path = Path(tmp_dir) / f"{name}.db"
    repo = Repository(db_path)
    return ResearchTaskOrchestrator(repo, TestDoubleProvider())


def section(title: str) -> None:
    print("\n" + "=" * 10 + f" {title} " + "=" * 10)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Successful autonomous run.
        orch = new_orchestrator(tmp, "success")
        task = orch.create_task(
            question_text=(
                "Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI infrastructure risk."
            ),
            created_by="analyst_1",
        )
        task = orch.run_autonomous_research(task.task_id)
        section("1. SUCCESSFUL STATE PATH")
        print(f"Final state: {task.state.value}")
        transitions = orch.repository.list_state_transitions_for_task(task.task_id)
        print(" -> ".join([transitions[0].from_state.value] + [t.to_state.value for t in transitions]))
        orch.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
        task = orch.publish(task.task_id)
        transitions = orch.repository.list_state_transitions_for_task(task.task_id)
        print("Full path incl. review/publish:")
        print(" -> ".join([transitions[0].from_state.value] + [t.to_state.value for t in transitions]))

        section("1b. ROUTING TRACE (from the successful run)")
        trace = assemble_trace(task.task_id, orch.repository)
        print(json.dumps(trace["routing_decisions"], indent=2))
        print("\nInterpreted scope:")
        print(json.dumps(trace["interpreted_scope"], indent=2))

        # 2. Clarification path.
        orch2 = new_orchestrator(tmp, "clarify")
        task2 = orch2.create_task(
            question_text="Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI exposure.",
            created_by="analyst_1",
        )
        task2 = orch2.run_autonomous_research(task2.task_id)
        section("2. CLARIFICATION PATH")
        print(f"State after first pass: {task2.state.value}")
        print(f"pending_clarification: {task2.pending_clarification}")
        task2 = orch2.submit_clarification_answer(task2.task_id, {"comparison_axis": "risk"})
        print(f"State after clarification answer: {task2.state.value}")
        transitions2 = orch2.repository.list_state_transitions_for_task(task2.task_id)
        print(" -> ".join([transitions2[0].from_state.value] + [t.to_state.value for t in transitions2]))

        # 3. Abstention path (one-sided evidence).
        orch3 = new_orchestrator(tmp, "abstain")
        task3 = orch3.create_task(
            question_text="Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI infrastructure risk.",
            created_by="analyst_1",
        )
        task3 = orch3.interpret_and_route(task3.task_id)
        evidence_by_subq = orch3.route_and_gather_evidence(task3.task_id)
        for subq, ev_list in evidence_by_subq.items():
            evidence_by_subq[subq] = [e for e in ev_list if not e.source_document_id.startswith("FABRIKAM")]
        orch3.complete_evidence_gathering(task3.task_id)
        orch3.draft_claims_for_subquestions(task3.task_id, evidence_by_subq, task3.created_by)
        orch3.complete_synthesis(task3.task_id)
        orch3.run_validation(task3.task_id)
        task3 = orch3.evaluate_sufficiency_and_advance(task3.task_id)
        section("3. ABSTENTION PATH")
        print(f"Final state: {task3.state.value}")
        print(f"abstention_reason: {task3.resolved_scope['abstention_reason']}")
        print("sufficiency_result:")
        print(json.dumps(task3.resolved_scope["sufficiency_result"], indent=2))

        orch.repository.close()
        orch2.repository.close()
        orch3.repository.close()


if __name__ == "__main__":
    main()
