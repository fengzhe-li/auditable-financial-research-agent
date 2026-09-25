"""Direct tests of the deterministic validator, independent of the
orchestrator - construct Evidence/Claim/ClaimEvidenceLink rows directly
against a Repository and check validate_claim()'s output.
"""

from __future__ import annotations

from afra.domain.enums import SupportContribution, SupportStatus
from afra.domain.models import Claim, ClaimEvidenceLink, Evidence, ResearchTask
from afra.validation.validator import validate_claim


def _ensure_task(repository, task_id):
    task = ResearchTask(question_text="q", created_by="analyst_1")
    task.task_id = task_id
    repository.save_task(task)
    return task


def _make_claim_with_evidence(repository, task_id, raw_text, quote_span, contribution):
    _ensure_task(repository, task_id)
    evidence = Evidence(
        task_id=task_id,
        source_document_id="DOC-1",
        source_location="DOC-1#section",
        raw_text=raw_text,
    )
    repository.save_evidence(evidence)
    claim = Claim(task_id=task_id, claim_text="a claim", model_id="test", created_by="analyst_1")
    repository.save_claim(claim)
    repository.save_link(
        ClaimEvidenceLink(
            claim_id=claim.claim_id,
            evidence_id=evidence.evidence_id,
            quote_span=quote_span,
            support_contribution=contribution,
        )
    )
    return claim, evidence


def test_claim_with_no_linked_evidence_is_insufficient(repository):
    _ensure_task(repository, "task-1")
    claim = Claim(task_id="task-1", claim_text="x", model_id="test", created_by="analyst_1")
    repository.save_claim(claim)

    result = validate_claim(claim.claim_id, repository)

    assert result.computed_support_status == SupportStatus.INSUFFICIENT_EVIDENCE
    assert result.missing_evidence_description


def test_claim_with_verbatim_supporting_quote_is_supported(repository):
    source = "Capital expenditure on AI infrastructure is expected to increase materially."
    claim, _ = _make_claim_with_evidence(
        repository,
        "task-1",
        source,
        "expected to increase materially",
        SupportContribution.SUPPORTS,
    )

    result = validate_claim(claim.claim_id, repository)

    assert result.computed_support_status == SupportStatus.SUPPORTED
    assert result.citation_check_passed
    assert result.computed_support_strength > 0


def test_claim_citing_text_not_present_in_evidence_is_unsupported(repository):
    source = "Capital expenditure on AI infrastructure is expected to increase materially."
    claim, _ = _make_claim_with_evidence(
        repository,
        "task-1",
        source,
        "revenue will decline sharply next year",
        SupportContribution.SUPPORTS,
    )

    result = validate_claim(claim.claim_id, repository)

    assert result.computed_support_status == SupportStatus.UNSUPPORTED
    assert not result.citation_check_passed


def test_claim_with_contradicting_evidence_is_unsupported(repository):
    source = "We do not expect AI infrastructure spending to increase in the near term."
    claim, _ = _make_claim_with_evidence(
        repository,
        "task-1",
        source,
        "do not expect AI infrastructure spending to increase",
        SupportContribution.CONTRADICTS,
    )

    result = validate_claim(claim.claim_id, repository)

    assert result.computed_support_status == SupportStatus.UNSUPPORTED
    assert result.conflicting_evidence_ids


def test_claim_with_both_supporting_and_contradicting_evidence_is_conflicting(repository):
    task_id = "task-1"
    _ensure_task(repository, task_id)
    claim = Claim(task_id=task_id, claim_text="x", model_id="test", created_by="analyst_1")
    repository.save_claim(claim)

    supporting_text = "AI infrastructure capex is expected to increase materially."
    contradicting_text = "AI infrastructure capex is expected to remain flat this year."

    supporting_evidence = Evidence(
        task_id=task_id, source_document_id="DOC-A", source_location="DOC-A#s", raw_text=supporting_text
    )
    contradicting_evidence = Evidence(
        task_id=task_id, source_document_id="DOC-B", source_location="DOC-B#s", raw_text=contradicting_text
    )
    repository.save_evidence(supporting_evidence)
    repository.save_evidence(contradicting_evidence)

    repository.save_link(
        ClaimEvidenceLink(
            claim_id=claim.claim_id,
            evidence_id=supporting_evidence.evidence_id,
            quote_span="expected to increase materially",
            support_contribution=SupportContribution.SUPPORTS,
        )
    )
    repository.save_link(
        ClaimEvidenceLink(
            claim_id=claim.claim_id,
            evidence_id=contradicting_evidence.evidence_id,
            quote_span="expected to remain flat this year",
            support_contribution=SupportContribution.CONTRADICTS,
        )
    )

    result = validate_claim(claim.claim_id, repository)

    assert result.computed_support_status == SupportStatus.CONFLICTING_EVIDENCE


def test_validation_result_independent_of_claim_drafting_model():
    """The validator's public signature takes only claim_id + repository -
    it cannot see, and does not depend on, which model (if any) drafted the
    claim text. This test asserts that property structurally: the function
    has no model/provider parameter at all.
    """
    import inspect

    sig = inspect.signature(validate_claim)
    assert list(sig.parameters) == ["claim_id", "repository"]
