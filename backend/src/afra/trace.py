"""assemble_trace() - one small, pure, read-only function that packages a
task's persisted state into the structure implied by the original AUDIT
TRACE shape (plan, tool_calls, retrieved_evidence, draft_claims,
validation_results, security_events, final_status, model_id, latency_ms,
tokens, cost).

Deliberately not a "TraceService": no caching, no pagination, no write
path, nothing beyond querying existing Repository methods and returning a
dict. See docs/PHASE_2_REPORT.md's overengineering section for why nothing
more than this exists yet - Phase 7 (frontend) is what would need a real
query/rendering layer for this, and building one now would be ahead of any
actual consumer.

Phase 3 additions: interpreted_scope, unresolved_fields,
clarification_responses, routing_decisions (a filtered view of tool_calls
where tool_name == "route_subquestion"), sufficiency_result, and
abstention_reason - all read directly from ResearchTask.resolved_scope,
where the orchestrator already persists them (no new storage added for the
trace itself).

Phase 4 addition: security_events is now a real, populated list (one entry
per persisted afra.domain.models.SecurityEvent), replacing the empty
placeholder from Phase 1-3.
"""

from __future__ import annotations

from afra.domain.claim_lifecycle import compute_effective_claims
from afra.storage.repository import Repository


def assemble_trace(task_id: str, repository: Repository) -> dict:
    task = repository.get_task(task_id)
    model_calls = repository.list_model_calls_for_task(task_id)
    tool_calls = repository.list_tool_calls_for_task(task_id)
    evidence = repository.list_evidence_for_task(task_id)
    claims = repository.list_claims_for_task(task_id)
    transitions = repository.list_state_transitions_for_task(task_id)
    reviews = repository.list_reviews_for_task(task_id)
    security_events = repository.list_security_events_for_task(task_id)
    effective_claim_ids = {c.claim_id for c in compute_effective_claims(claims)}

    claim_trace = []
    for claim in claims:
        links = repository.list_links_for_claim(claim.claim_id)
        validations = repository.list_validation_results_for_claim(claim.claim_id)
        objections = repository.list_objections_for_claim(claim.claim_id)
        claim_trace.append(
            {
                "claim_id": claim.claim_id,
                "claim_text": claim.claim_text,
                "model_id": claim.model_id,
                "support_status": claim.support_status.value if claim.support_status else None,
                "support_strength": claim.support_strength,
                "approval_status": claim.approval_status.value,
                # Revision semantics - see
                # docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics.
                # "is_effective" is the same predicate
                # afra.domain.claim_lifecycle.compute_effective_claims
                # applies, surfaced per-claim so the frontend never has to
                # recompute it.
                "claim_set_version": claim.claim_set_version,
                "lifecycle_status": claim.lifecycle_status.value,
                "is_effective": claim.claim_id in effective_claim_ids,
                "predecessor_claim_id": claim.predecessor_claim_id,
                "successor_claim_id": claim.successor_claim_id,
                "withdrawn_reason": claim.withdrawn_reason,
                "objections": [
                    {
                        "objection_id": o.objection_id,
                        "reviewer_id": o.reviewer_id,
                        "reason": o.reason,
                        "claim_set_version": o.claim_set_version,
                        "created_at": o.created_at.isoformat() if o.created_at else None,
                    }
                    for o in objections
                ],
                "evidence_links": [
                    {
                        "evidence_id": link.evidence_id,
                        "quote_span": link.quote_span,
                        "support_contribution": link.support_contribution.value,
                    }
                    for link in links
                ],
                "validation_results": [
                    {
                        "validation_id": v.validation_id,
                        "computed_support_status": v.computed_support_status.value,
                        "citation_check_passed": v.citation_check_passed,
                        "missing_evidence_description": v.missing_evidence_description,
                    }
                    for v in validations
                ],
            }
        )

    return {
        "task_id": task.task_id,
        "policy_id": task.policy_id,
        "policy_version": task.policy_version,
        "publication_decisions": repository.list_publication_decisions(task_id),
        "question_text": task.question_text,
        "interpreted_scope": task.resolved_scope.get("research_scope"),
        "unresolved_fields": task.pending_clarification,
        "clarification_responses": task.resolved_scope.get("clarification_responses", []),
        "plan": task.resolved_scope.get("subquestions", []),
        "tool_calls": [
            {
                "tool_name": tc.tool_name,
                "input_summary": tc.input_summary,
                "output_summary": tc.output_summary,
                "succeeded": tc.succeeded,
            }
            for tc in tool_calls
        ],
        "routing_decisions": [
            {
                "subquestion": tc.input_summary,
                "decision": tc.output_summary,
            }
            for tc in tool_calls
            if tc.tool_name == "route_subquestion"
        ],
        "sufficiency_result": task.resolved_scope.get("sufficiency_result"),
        "abstention_reason": task.resolved_scope.get("abstention_reason"),
        "retrieved_evidence": [
            {
                "evidence_id": e.evidence_id,
                "source_document_id": e.source_document_id,
                "source_location": e.source_location,
                "classification": e.classification.value,
            }
            for e in evidence
        ],
        "draft_claims": claim_trace,
        # The currently effective claim set (afra.domain.claim_lifecycle
        # .compute_effective_claims), as claim_ids - "draft_claims" above is
        # deliberately the full historical list (backward compatible with
        # every existing consumer); this is the narrower, revision-aware
        # view described in docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics.
        "effective_claim_ids": sorted(effective_claim_ids),
        "security_events": [
            {
                "trigger": se.trigger,
                "classification": se.classification.value,
                "action_taken": se.action_taken.value,
                "detected_categories": se.detected_categories,
                "requested_provider_id": se.requested_provider_id,
                "selected_provider_id": se.selected_provider_id,
                "policy_result": se.policy_result,
                "policy_id": se.policy_id,
                "policy_version": se.policy_version,
                "resolved_by": se.resolved_by,
            }
            for se in security_events
        ],
        "claim_set_version": task.claim_set_version,
        "reviews": [
            {
                "reviewer_id": r.reviewer_id,
                "policy_id": r.policy_id,
                "policy_version": r.policy_version,
                "decision": r.decision.value,
                "comments": r.comments,
                "claim_set_version": r.claim_set_version,
                "security_context": r.security_context,
                "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            }
            for r in reviews
        ],
        "state_transitions": [
            {"from_state": t.from_state.value, "to_state": t.to_state.value}
            for t in transitions
        ],
        "final_status": task.state.value,
        "model_calls": [
            {
                "purpose": mc.purpose,
                "provider_id": mc.provider_id,
                "model_id": mc.model_id,
                "latency_ms": mc.latency_ms,
                "tokens_in": mc.tokens_in,
                "tokens_out": mc.tokens_out,
                "cost": mc.cost,
            }
            for mc in model_calls
        ],
        "total_latency_ms": sum(mc.latency_ms for mc in model_calls),
        "total_tokens": sum(mc.tokens_in + mc.tokens_out for mc in model_calls),
        "total_cost": sum(mc.cost for mc in model_calls),
    }
