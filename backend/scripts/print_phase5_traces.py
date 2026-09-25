"""Runs four Phase 5 scenarios against throwaway SQLite files and prints
each one's outcome: an approve -> publish path, a request-revision ->
revalidate -> reapprove path, a stale-approval rejection, and a
SECURITY_BLOCKED case that ordinary approval cannot bypass. Used to
generate the examples in docs/PHASE_5_REPORT.md - not part of the test
suite.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from afra.domain.enums import ReviewDecision, SupportContribution, TaskState
from afra.domain.errors import IllegalTransitionError, PublicationGateError, SecurityPolicyError
from afra.domain.models import Claim, Review, ResearchTask, ValidationResult
from afra.orchestrator.orchestrator import EvidenceLinkSpec, ResearchTaskOrchestrator
from afra.providers.test_double import TestDoubleProvider
from afra.review.publication_gate import evaluate_publication_gate
from afra.storage.repository import Repository
from afra.trace import assemble_trace


def new_orchestrator(tmp_dir: str, name: str) -> ResearchTaskOrchestrator:
    db_path = Path(tmp_dir) / f"{name}.db"
    repo = Repository(db_path)
    return ResearchTaskOrchestrator(repo, TestDoubleProvider())


def section(title: str) -> None:
    print("\n" + "=" * 10 + f" {title} " + "=" * 10)


def to_awaiting_review(orch, quote_span="increase materially"):
    task = orch.create_task(
        question_text="What AI infrastructure investment risk does Contoso Cloud Corp disclose?",
        created_by="analyst_1",
    )
    orch.mark_plan_complete(task.task_id)
    evidence = orch.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")
    orch.complete_evidence_gathering(task.task_id)
    orch.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp expects AI infrastructure capex to increase materially.",
        evidence_links=[EvidenceLinkSpec(evidence.evidence_id, quote_span, SupportContribution.SUPPORTS)],
        created_by="analyst_1",
    )
    orch.complete_synthesis(task.task_id)
    orch.run_validation(task.task_id)
    task = orch.evaluate_sufficiency_and_advance(task.task_id)
    return task, evidence


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Approve -> publish.
        orch1 = new_orchestrator(tmp, "approve")
        task1, _ = to_awaiting_review(orch1)
        task1 = orch1.submit_review(task1.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
        task1 = orch1.publish(task1.task_id)
        section("1. APPROVE -> PUBLISH PATH")
        transitions1 = orch1.repository.list_state_transitions_for_task(task1.task_id)
        print(" -> ".join([transitions1[0].from_state.value] + [t.to_state.value for t in transitions1]))
        reviews1 = orch1.repository.list_reviews_for_task(task1.task_id)
        print(json.dumps(
            [{"decision": r.decision.value, "reviewer_id": r.reviewer_id, "claim_set_version": r.claim_set_version} for r in reviews1],
            indent=2,
        ))

        # 2. Request-revision -> revalidate -> reapprove.
        orch2 = new_orchestrator(tmp, "revise")
        task2, evidence2 = to_awaiting_review(orch2)
        orch2.submit_review(
            task2.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.REQUEST_REVISION,
            comments="Add a second corroborating citation.",
        )
        task2 = orch2.resume_from_revision(task2.task_id, needs_new_evidence=False)
        orch2.draft_claim(
            task2.task_id,
            prompt="Additional corroborating context on Contoso Cloud Corp's AI capex plans.",
            evidence_links=[EvidenceLinkSpec(evidence2.evidence_id, "increase materially", SupportContribution.SUPPORTS)],
            created_by="analyst_1",
        )
        orch2.complete_synthesis(task2.task_id)
        orch2.run_validation(task2.task_id)
        task2 = orch2.evaluate_sufficiency_and_advance(task2.task_id)
        task2 = orch2.submit_review(task2.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
        task2 = orch2.publish(task2.task_id)
        section("2. REQUEST_REVISION -> REVALIDATE -> REAPPROVE PATH")
        transitions2 = orch2.repository.list_state_transitions_for_task(task2.task_id)
        print(" -> ".join([transitions2[0].from_state.value] + [t.to_state.value for t in transitions2]))
        reviews2 = orch2.repository.list_reviews_for_task(task2.task_id)
        print(json.dumps(
            [{"decision": r.decision.value, "claim_set_version": r.claim_set_version, "comments": r.comments} for r in reviews2],
            indent=2,
        ))

        # 3. Stale approval rejection (hand-constructed - no live path).
        repo3 = Repository(Path(tmp) / "stale.db")
        task3 = ResearchTask(question_text="q", created_by="analyst_1")
        task3.state = TaskState.APPROVED
        task3.claim_set_version = 2
        repo3.save_task(task3)
        claim3 = Claim(task_id=task3.task_id, claim_text="c", model_id="test", created_by="analyst_1")
        repo3.save_claim(claim3)
        from afra.domain.enums import SupportStatus

        repo3.record_validation_result(
            ValidationResult(claim_id=claim3.claim_id, computed_support_status=SupportStatus.SUPPORTED)
        )
        repo3.save_review(
            Review(task_id=task3.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE, claim_set_version=1)
        )
        gate_result3 = evaluate_publication_gate(task3.task_id, repo3)
        section("3. STALE-APPROVAL REJECTION")
        print(f"gate.passed={gate_result3.passed}")
        print(f"reasons={gate_result3.reasons}")
        repo3.close()

        # 4. SECURITY_BLOCKED cannot be bypassed by ordinary approval.
        orch4 = new_orchestrator(tmp, "blocked")
        task4 = orch4.create_task(question_text="What does Contoso Cloud Corp disclose?", created_by="analyst_1")
        orch4.mark_plan_complete(task4.task_id)
        evidence4 = orch4.gather_evidence_from_document(task4.task_id, "CONTOSO-MNPI-NOTE", "risk_factors")
        orch4.complete_evidence_gathering(task4.task_id)
        try:
            orch4.draft_claim(
                task4.task_id,
                prompt="Summarise the restricted note.",
                evidence_links=[EvidenceLinkSpec(evidence4.evidence_id, "material non-public information", SupportContribution.SUPPORTS)],
                created_by="analyst_1",
            )
        except SecurityPolicyError as exc:
            section("4. SECURITY_BLOCKED CANNOT BE BYPASSED BY ORDINARY APPROVAL")
            print(f"SecurityPolicyError raised during drafting: {exc}")
        reloaded4 = orch4.repository.get_task(task4.task_id)
        print(f"task.state={reloaded4.state.value}")
        try:
            orch4.submit_review(task4.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
        except IllegalTransitionError as exc:
            print(f"submit_review() raised IllegalTransitionError: {exc}")
        reloaded4 = orch4.repository.get_task(task4.task_id)
        print(f"task.state after attempted review={reloaded4.state.value} (unchanged)")

        orch1.repository.close()
        orch2.repository.close()
        orch4.repository.close()


if __name__ == "__main__":
    main()
