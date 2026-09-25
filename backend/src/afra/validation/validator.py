"""The one deterministic evidence validator for the Phase 1 vertical slice.

Deliberately independent of whichever model (real or test-double) drafted
the claim: this function takes only claim_id and a Repository, reads the
authoritative ClaimEvidenceLink rows (never a denormalised evidence_ids[]
view - see docs/CLAIM_EVIDENCE_MODEL.md#claimevidence_ids-vs-claimevidencelink),
and computes support_status from citation-checkable facts. No model call
happens here at all in Phase 1 - a later phase's validator may use a model
for entailment *assistance*, but that would still be a separate call from
whatever call drafted the claim, and its output would still only ever
become authoritative by passing through record_validation_result().

This is a small, intentionally simple validator - it proves the
architectural property (support_status is computed, not self-reported), not
a sophisticated entailment engine. Generalising it is Phase 3 scope; see
docs/ROADMAP.md.
"""

from __future__ import annotations

import re

from afra.domain.enums import SupportContribution, SupportStatus
from afra.domain.models import ValidationResult
from afra.storage.repository import Repository


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _quote_is_supported_by(quote_span: str, source_text: str) -> bool:
    """Citation check: is the cited span actually present in the source
    text, modulo whitespace/case normalisation? See
    docs/THREAT_MODEL.md#citation-mismatch.
    """
    return _normalise(quote_span) in _normalise(source_text)


def validate_claim(claim_id: str, repository: Repository) -> ValidationResult:
    links = repository.list_links_for_claim(claim_id)

    if not links:
        return ValidationResult(
            claim_id=claim_id,
            computed_support_status=SupportStatus.INSUFFICIENT_EVIDENCE,
            computed_support_strength=0.0,
            citation_check_passed=False,
            missing_evidence_description=(
                "No evidence is linked to this claim. A claim cannot be "
                "validated without at least one ClaimEvidenceLink."
            ),
        )

    valid_supporting: list[str] = []
    valid_contradicting: list[str] = []
    invalid_citations: list[str] = []

    for link in links:
        evidence = repository.get_evidence(link.evidence_id)
        citation_ok = _quote_is_supported_by(link.quote_span, evidence.raw_text)
        if not citation_ok:
            invalid_citations.append(link.evidence_id)
            continue
        if link.support_contribution == SupportContribution.SUPPORTS:
            valid_supporting.append(link.evidence_id)
        elif link.support_contribution == SupportContribution.CONTRADICTS:
            valid_contradicting.append(link.evidence_id)
        # NEUTRAL links pass the citation check but don't move the needle
        # on support either way.

    citation_check_passed = not invalid_citations

    if valid_supporting and valid_contradicting:
        return ValidationResult(
            claim_id=claim_id,
            computed_support_status=SupportStatus.CONFLICTING_EVIDENCE,
            computed_support_strength=0.5,
            citation_check_passed=citation_check_passed,
            conflicting_evidence_ids=valid_contradicting,
            missing_evidence_description=None,
        )

    if valid_supporting and not valid_contradicting:
        strength = min(1.0, 0.5 + 0.25 * len(valid_supporting))
        status = (
            SupportStatus.SUPPORTED
            if citation_check_passed
            else SupportStatus.PARTIALLY_SUPPORTED
        )
        return ValidationResult(
            claim_id=claim_id,
            computed_support_status=status,
            computed_support_strength=strength,
            citation_check_passed=citation_check_passed,
            missing_evidence_description=(
                None
                if citation_check_passed
                else "Some linked evidence citations could not be verified against source text."
            ),
        )

    if valid_contradicting and not valid_supporting:
        return ValidationResult(
            claim_id=claim_id,
            computed_support_status=SupportStatus.UNSUPPORTED,
            computed_support_strength=0.0,
            citation_check_passed=citation_check_passed,
            conflicting_evidence_ids=valid_contradicting,
            missing_evidence_description=(
                "Linked evidence contradicts this claim; no supporting evidence found."
            ),
        )

    # No valid supporting or contradicting links survived the citation
    # check (only invalid citations, and/or only neutral links).
    if invalid_citations:
        return ValidationResult(
            claim_id=claim_id,
            computed_support_status=SupportStatus.UNSUPPORTED,
            computed_support_strength=0.0,
            citation_check_passed=False,
            missing_evidence_description=(
                "Cited evidence text does not contain the quoted span; "
                "citation could not be verified."
            ),
        )

    return ValidationResult(
        claim_id=claim_id,
        computed_support_status=SupportStatus.INSUFFICIENT_EVIDENCE,
        computed_support_strength=0.0,
        citation_check_passed=True,
        missing_evidence_description=(
            "Linked evidence is neutral; no evidence directly supports or "
            "contradicts this claim."
        ),
    )
