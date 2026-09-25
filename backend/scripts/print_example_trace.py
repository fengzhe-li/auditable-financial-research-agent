"""Runs the bounded comparison task end-to-end against a throwaway SQLite
file and prints its assembled trace as JSON. Used to generate the example
in docs/PHASE_2_REPORT.md - not part of the test suite, not imported by
anything else.
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


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "example.db"
        repo = Repository(db_path)
        orchestrator = ResearchTaskOrchestrator(repo, TestDoubleProvider())

        task = orchestrator.create_task(
            question_text=(
                "Compare how Contoso Cloud Corp and Fabrikam Systems Inc describe AI "
                "infrastructure investment risk across their latest two annual reports."
            ),
            created_by="analyst_1",
        )
        subquestions = orchestrator.run_planning(
            task.task_id, ["Contoso Cloud Corp", "Fabrikam Systems Inc"]
        )
        orchestrator.search_documents(task.task_id, company="Contoso Cloud Corp")
        orchestrator.search_documents(task.task_id, company="Fabrikam Systems Inc")
        contoso_evidence = orchestrator.gather_evidence_from_document(
            task.task_id, "CONTOSO-2025-10K", "risk_factors"
        )
        fabrikam_evidence = orchestrator.gather_evidence_from_document(
            task.task_id, "FABRIKAM-2025-10K", "risk_factors"
        )
        orchestrator.complete_evidence_gathering(task.task_id)

        orchestrator.draft_claim_from_evidence(task.task_id, subquestions[0], [contoso_evidence], "analyst_1")
        orchestrator.draft_claim_from_evidence(task.task_id, subquestions[1], [fabrikam_evidence], "analyst_1")
        orchestrator.draft_claim_from_evidence(
            task.task_id, subquestions[2], [contoso_evidence, fabrikam_evidence], "analyst_1"
        )
        orchestrator.complete_synthesis(task.task_id)
        orchestrator.run_validation(task.task_id)
        orchestrator.evaluate_sufficiency_and_advance(task.task_id)
        orchestrator.submit_review(task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE)
        orchestrator.publish(task.task_id)

        trace = assemble_trace(task.task_id, repo)
        print(json.dumps(trace, indent=2))
        repo.close()


if __name__ == "__main__":
    main()
