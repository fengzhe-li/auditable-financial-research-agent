"""Populates a demo SQLite database (AFRA_DB_PATH, default
backend/afra_demo.db) with a handful of ResearchTasks in genuinely distinct
states, so the Phase 7 frontend has real, varied, inspectable data to
render - see docs/ROADMAP.md's Phase 7 entry.

Every task below is produced by calling real, unmodified
ResearchTaskOrchestrator methods against the existing fixture corpus and
deterministic test-double providers (afra.providers.test_double) - the same
methods and providers Phase 1-6's own tests and benchmark harness use.
Nothing here is a frontend-only fixture or a new backend code path: this
script is a caller of the existing orchestrator API, exactly like
afra/benchmark/configurations.py is. No real external LLM is used or
importable from this script.

Two tasks (INSUFFICIENT_EVIDENCE and SECURITY_BLOCKED) reuse the exact
manual-evidence-construction technique docs/PHASE_6_REPORT.md's benchmark
harness uses (afra.benchmark.configurations' manual_conflicting_claim /
manual_document_override patterns) - necessary because
afra.routing.router.pick_latest_document() cannot autonomously reach a
genuine cross-year contradiction or the RESTRICTED fixture document (a
documented Phase 3/6 router limitation, not something this script works
around silently). One task (a "stale approval") reuses the exact
hand-construction technique tests/test_phase5_review_workflow.py's
test_stale_approval_cannot_authorize_a_modified_claim_set uses - there is
no live orchestrator path from APPROVED back to a modified claim set (see
that test's docstring), so directly bumping claim_set_version after a real
approval is the same, established way to demonstrate the rule.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from afra.domain.enums import ProviderClass, ReviewDecision, SupportContribution, TaskState
from afra.domain.errors import SecurityPolicyError
from afra.orchestrator.orchestrator import EvidenceLinkSpec, ResearchTaskOrchestrator
from afra.providers.test_double import (
    EnterpriseApprovedTestDoubleProvider,
    PrivateLocalTestDoubleProvider,
    TestDoubleProvider,
)
from afra.storage.repository import Repository

COMPARISON_QUESTION = "Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI infrastructure risk."
AMBIGUOUS_AXIS_QUESTION = "Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI exposure."
FABRIKAM_QUESTION = "What does Fabrikam Systems Inc disclose about AI infrastructure investment risk?"
CONTOSO_QUESTION = "What does Contoso Cloud Corp disclose about AI infrastructure investment risk?"
RESTRICTED_QUESTION = "What pending AI infrastructure financing arrangement does Contoso Cloud Corp's restricted note describe?"


def _provider_registry():
    external = TestDoubleProvider()
    enterprise = EnterpriseApprovedTestDoubleProvider()
    private = PrivateLocalTestDoubleProvider()
    return {
        external.provider_class: external,
        enterprise.provider_class: enterprise,
        private.provider_class: private,
    }


def _orchestrator(repository: Repository) -> ResearchTaskOrchestrator:
    providers = _provider_registry()
    return ResearchTaskOrchestrator(repository, providers[ProviderClass.EXTERNAL_STANDARD], providers=providers)


def seed_published(repository: Repository) -> str:
    orchestrator = _orchestrator(repository)
    task = orchestrator.create_task(question_text=COMPARISON_QUESTION, created_by="analyst_1")
    task = orchestrator.run_autonomous_research(task.task_id)
    assert task.state == TaskState.AWAITING_REVIEW, task.state
    task = orchestrator.submit_review(
        task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE,
        comments="Evidence-backed and consistent across both companies' latest filings.",
    )
    task = orchestrator.publish(task.task_id)
    assert task.state == TaskState.PUBLISHED, task.state
    return task.task_id


def seed_awaiting_review(repository: Repository) -> str:
    orchestrator = _orchestrator(repository)
    task = orchestrator.create_task(question_text=FABRIKAM_QUESTION, created_by="analyst_1")
    task = orchestrator.run_autonomous_research(task.task_id)
    assert task.state == TaskState.AWAITING_REVIEW, task.state
    return task.task_id


def seed_needs_clarification(repository: Repository) -> str:
    orchestrator = _orchestrator(repository)
    task = orchestrator.create_task(question_text=AMBIGUOUS_AXIS_QUESTION, created_by="analyst_1")
    task = orchestrator.run_autonomous_research(task.task_id)
    assert task.state == TaskState.NEEDS_CLARIFICATION, task.state
    return task.task_id


def seed_rejected(repository: Repository) -> str:
    orchestrator = _orchestrator(repository)
    task = orchestrator.create_task(question_text=CONTOSO_QUESTION, created_by="analyst_1")
    task = orchestrator.run_autonomous_research(task.task_id)
    assert task.state == TaskState.AWAITING_REVIEW, task.state
    task = orchestrator.submit_review(
        task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.REJECT,
        comments="Scope is too narrow to be useful as a standalone published finding.",
    )
    assert task.state == TaskState.REJECTED, task.state
    return task.task_id


def seed_revision_requested(repository: Repository) -> str:
    orchestrator = _orchestrator(repository)
    task = orchestrator.create_task(
        question_text="What does Contoso Cloud Corp disclose about AI infrastructure investment risk in its most recent annual report?",
        created_by="analyst_1",
    )
    task = orchestrator.run_autonomous_research(task.task_id)
    assert task.state == TaskState.AWAITING_REVIEW, task.state
    task = orchestrator.submit_review(
        task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.REQUEST_REVISION,
        comments="Please also cite the prior-year filing for comparison before this is publishable.",
    )
    assert task.state == TaskState.REVISION_REQUESTED, task.state
    return task.task_id


def seed_stale_approval(repository: Repository) -> str:
    """A real APPROVED task whose approval is then made stale by a further
    claim_set_version bump - see this module's docstring for why this
    hand-construction (not a live orchestrator path) is the correct way to
    demonstrate the rule."""
    orchestrator = _orchestrator(repository)
    task = orchestrator.create_task(
        question_text="What does Fabrikam Systems Inc disclose about AI infrastructure investment risk in its most recent annual report?",
        created_by="analyst_1",
    )
    task = orchestrator.run_autonomous_research(task.task_id)
    assert task.state == TaskState.AWAITING_REVIEW, task.state
    task = orchestrator.submit_review(
        task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE,
        comments="Approved as of claim_set_version 1.",
    )
    assert task.state == TaskState.APPROVED, task.state
    task = repository.get_task(task.task_id)
    task.claim_set_version += 1  # simulates the claim set changing after approval
    repository.save_task(task)
    return task.task_id


def seed_insufficient_evidence(repository: Repository) -> str:
    """A genuine cross-year contradiction (CONTOSO-2024-10K vs
    CONTOSO-2025-10K capex outlook) - reuses the exact manual claim
    construction docs/PHASE_6_REPORT.md's benchmark harness (CE-01) and
    tests/test_phase3_end_to_end.py already established, since
    pick_latest_document() alone cannot reach a genuine same-claim,
    cross-year contradiction."""
    orchestrator = _orchestrator(repository)
    task = orchestrator.create_task(
        question_text="Does Contoso Cloud Corp expect material AI infrastructure capex growth?",
        created_by="analyst_1",
    )
    task = orchestrator.mark_plan_complete(task.task_id)
    evidence_2024 = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2024-10K", "risk_factors")
    evidence_2025 = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)
    orchestrator.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp does not expect material AI infrastructure capital expenditure growth.",
        evidence_links=[
            EvidenceLinkSpec(
                evidence_2024.evidence_id,
                "do not currently expect this to require a material increase",
                SupportContribution.SUPPORTS,
            ),
            EvidenceLinkSpec(
                evidence_2025.evidence_id,
                "expect this capital expenditure to increase materially",
                SupportContribution.CONTRADICTS,
            ),
        ],
        created_by="analyst_1",
    )
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    task = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    assert task.state == TaskState.INSUFFICIENT_EVIDENCE, task.state
    return task.task_id


def seed_security_blocked(repository: Repository) -> str:
    """Asks about the RESTRICTED fixture document directly (autonomous
    routing never reaches it - see docs/PHASE_6_REPORT.md's §11), so
    afra.policy.enforcement blocks before any model call, matching the
    benchmark's SD-03/SD-04 pattern."""
    orchestrator = _orchestrator(repository)
    task = orchestrator.create_task(question_text=RESTRICTED_QUESTION, created_by="analyst_1")
    task = orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-MNPI-NOTE", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)
    try:
        orchestrator.draft_claim_from_evidence(task.task_id, RESTRICTED_QUESTION, [evidence], created_by="analyst_1")
    except SecurityPolicyError:
        pass
    task = repository.get_task(task.task_id)
    assert task.state == TaskState.SECURITY_BLOCKED, task.state
    return task.task_id


def main() -> None:
    db_path = os.environ.get("AFRA_DB_PATH", str(Path(__file__).resolve().parents[1] / "afra_demo.db"))
    print(f"Seeding {db_path}")
    repository = Repository(db_path)
    try:
        seeded = {
            "PUBLISHED": seed_published(repository),
            "AWAITING_REVIEW": seed_awaiting_review(repository),
            "NEEDS_CLARIFICATION": seed_needs_clarification(repository),
            "REJECTED": seed_rejected(repository),
            "REVISION_REQUESTED": seed_revision_requested(repository),
            "APPROVED (stale approval)": seed_stale_approval(repository),
            "INSUFFICIENT_EVIDENCE": seed_insufficient_evidence(repository),
            "SECURITY_BLOCKED": seed_security_blocked(repository),
        }
    finally:
        repository.close()

    for label, task_id in seeded.items():
        print(f"  {label:28s} {task_id}")


if __name__ == "__main__":
    main()
