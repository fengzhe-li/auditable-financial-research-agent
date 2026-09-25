"""Direct unit tests of compute_sufficiency(), constructed by hand against
the repository - same pattern as test_publication_gate.py.
"""

from __future__ import annotations

from afra.domain.enums import SupportContribution, SupportStatus
from afra.domain.models import Claim, ClaimEvidenceLink, Evidence, ResearchTask, ValidationResult
from afra.sufficiency.coverage import compute_sufficiency


def _task_with_scope(repository, companies=None, subquestions=None):
    task = ResearchTask(question_text="q", created_by="analyst_1")
    if companies is not None:
        task.resolved_scope["research_scope"] = {
            "companies": {"status": "RESOLVED", "value": companies, "reason": None}
        }
    if subquestions is not None:
        task.resolved_scope["subquestions"] = subquestions
    repository.save_task(task)
    return task


def _claim_with_evidence(repository, task_id, subquestion, document_id, status):
    evidence = Evidence(task_id=task_id, source_document_id=document_id, source_location=f"{document_id}#s", raw_text="text")
    repository.save_evidence(evidence)
    claim = Claim(task_id=task_id, claim_text="c", model_id="m", created_by="analyst_1", subquestion=subquestion)
    repository.save_claim(claim)
    repository.save_link(
        ClaimEvidenceLink(
            claim_id=claim.claim_id, evidence_id=evidence.evidence_id, quote_span="text",
            support_contribution=SupportContribution.SUPPORTS,
        )
    )
    repository.record_validation_result(ValidationResult(claim_id=claim.claim_id, computed_support_status=status))
    return claim


def test_fully_covered_two_sided_comparison_is_sufficient(repository):
    task = _task_with_scope(
        repository,
        companies=["Contoso Cloud Corp", "Fabrikam Systems Inc"],
        subquestions=["Q1 about Contoso", "Q2 about Fabrikam"],
    )
    _claim_with_evidence(repository, task.task_id, "Q1 about Contoso", "CONTOSO-2025-10K", SupportStatus.SUPPORTED)
    _claim_with_evidence(repository, task.task_id, "Q2 about Fabrikam", "FABRIKAM-2025-10K", SupportStatus.SUPPORTED)

    result = compute_sufficiency(task, repository)

    assert result["is_sufficient"] is True
    assert result["comparison_sides_represented"] is True
    assert result["missing_sides"] == []


def test_one_sided_comparison_is_insufficient(repository):
    task = _task_with_scope(
        repository,
        companies=["Contoso Cloud Corp", "Fabrikam Systems Inc"],
        subquestions=["Q1 about Contoso"],
    )
    _claim_with_evidence(repository, task.task_id, "Q1 about Contoso", "CONTOSO-2025-10K", SupportStatus.SUPPORTED)
    # No Fabrikam evidence anywhere on this task.

    result = compute_sufficiency(task, repository)

    assert result["is_sufficient"] is False
    assert result["comparison_sides_represented"] is False
    assert result["missing_sides"] == ["Fabrikam Systems Inc"]
    assert "Fabrikam" in result["reason"]


def test_uncovered_subquestion_is_insufficient(repository):
    task = _task_with_scope(repository, subquestions=["Q1", "Q2"])
    _claim_with_evidence(repository, task.task_id, "Q1", "CONTOSO-2025-10K", SupportStatus.SUPPORTED)
    # No claim at all for Q2.

    result = compute_sufficiency(task, repository)

    assert result["is_sufficient"] is False
    assert result["subquestions_covered"] == {"Q1": True, "Q2": False}


def test_unresolved_contradiction_is_insufficient(repository):
    task = _task_with_scope(repository, subquestions=["Q1"])
    _claim_with_evidence(repository, task.task_id, "Q1", "CONTOSO-2025-10K", SupportStatus.CONFLICTING_EVIDENCE)

    result = compute_sufficiency(task, repository)

    assert result["is_sufficient"] is False
    assert len(result["unresolved_contradictions"]) == 1
    assert "conflicting evidence" in result["reason"]


def test_claim_with_no_evidence_link_is_flagged(repository):
    task = ResearchTask(question_text="q", created_by="analyst_1")
    repository.save_task(task)
    claim = Claim(task_id=task.task_id, claim_text="c", model_id="m", created_by="analyst_1")
    repository.save_claim(claim)
    repository.record_validation_result(
        ValidationResult(claim_id=claim.claim_id, computed_support_status=SupportStatus.INSUFFICIENT_EVIDENCE)
    )

    result = compute_sufficiency(task, repository)

    assert result["is_sufficient"] is False
    assert claim.claim_id in result["claims_without_evidence"]
    assert claim.claim_id in result["insufficient_evidence_claims"]


def test_sufficiency_check_works_without_any_research_scope(repository):
    """Phase 1/2 tasks never set research_scope/subquestions at all - the
    check must degrade gracefully, not crash."""
    task = ResearchTask(question_text="q", created_by="analyst_1")
    repository.save_task(task)
    claim = Claim(task_id=task.task_id, claim_text="c", model_id="m", created_by="analyst_1")
    repository.save_claim(claim)
    evidence = Evidence(task_id=task.task_id, source_document_id="D", source_location="D#s", raw_text="text")
    repository.save_evidence(evidence)
    repository.save_link(
        ClaimEvidenceLink(
            claim_id=claim.claim_id, evidence_id=evidence.evidence_id, quote_span="text",
            support_contribution=SupportContribution.SUPPORTS,
        )
    )
    repository.record_validation_result(
        ValidationResult(claim_id=claim.claim_id, computed_support_status=SupportStatus.SUPPORTED)
    )

    result = compute_sufficiency(task, repository)

    assert result["is_sufficient"] is True
    assert result["comparison_sides_represented"] is None
