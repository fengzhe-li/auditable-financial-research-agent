"""Prompt construction for evidence-grounded claim drafting.

Deliberately just prompt construction - the actual drafting call, claim
persistence, and evidence-link persistence live in
Orchestrator.draft_claim_from_evidence(), the same pattern Phase 1 used for
draft_claim(). Keeping this module to pure prompt-building (no persistence,
no provider call) makes the one invariant this whole step exists to protect
easy to see in one place: nothing in this module, or in the provider
response it feeds, is ever allowed to set support_status, support_strength,
or approval_status - those fields do not appear anywhere near claim
drafting. They are written only by afra.storage.repository.Repository
.record_validation_result() (support_status/support_strength) and by the
review flow (approval_status) - see
docs/CLAIM_EVIDENCE_MODEL.md#validation-authority.
"""

from __future__ import annotations

from afra.domain.models import Evidence


def build_claim_prompt(subquestion: str, evidence_list: list[Evidence]) -> str:
    evidence_block = "\n\n".join(
        f'Evidence from {e.source_document_id} ({e.source_location}):\n"{e.raw_text}"'
        for e in evidence_list
    )
    return (
        f"Subquestion: {subquestion}\n\n"
        f"{evidence_block}\n\n"
        "Task: state one factual claim that directly answers the "
        "subquestion, using only the evidence above. Do not state whether "
        "the claim is verified - that is decided separately by an "
        "independent validator, not by you."
    )
