import type { TaskDetail, TaskSummary } from "../api/types";

// Realistic fixtures shaped exactly like afra.api.app's real JSON responses
// (verified against the live API during development) - kept in one place
// so page tests don't each hand-roll a slightly different, drifting shape.

export function makeTaskSummary(overrides: Partial<TaskSummary> = {}): TaskSummary {
  return {
    task_id: "task_abc123",
    question_text: "What does Contoso Cloud Corp disclose about AI infrastructure investment risk?",
    state: "PUBLISHED",
    created_by: "analyst_1",
    classification: "PUBLIC",
    claim_set_version: 1,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:05:00+00:00",
    publication_allowed: false,
    ...overrides,
  };
}

export function makeTaskDetail(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return {
    task_id: "task_abc123",
    question_text: "What does Contoso Cloud Corp disclose about AI infrastructure investment risk?",
    interpreted_scope: {
      companies: { status: "RESOLVED", value: ["Contoso Cloud Corp"], reason: null },
      comparison_axis: { status: "RESOLVED", value: null, reason: null },
    },
    unresolved_fields: null,
    clarification_responses: [],
    plan: ["What does Contoso Cloud Corp disclose about AI infrastructure investment risk?"],
    tool_calls: [
      { tool_name: "search_documents", input_summary: "company='Contoso Cloud Corp'", output_summary: "CONTOSO-2025-10K", succeeded: true },
    ],
    routing_decisions: [],
    sufficiency_result: { is_sufficient: true },
    abstention_reason: null,
    retrieved_evidence: [
      { evidence_id: "evidence_1", source_document_id: "CONTOSO-2025-10K", source_location: "CONTOSO-2025-10K#risk_factors", classification: "PUBLIC" },
    ],
    draft_claims: [
      {
        claim_id: "claim_1",
        claim_text: "Contoso Cloud Corp expects material capex growth.",
        model_id: "deterministic-echo-v1",
        support_status: "SUPPORTED",
        support_strength: 0.9,
        approval_status: "PENDING",
        evidence_links: [{ evidence_id: "evidence_1", quote_span: "expect this capital expenditure to increase", support_contribution: "supports" }],
        validation_results: [{ validation_id: "v1", computed_support_status: "SUPPORTED", citation_check_passed: true, missing_evidence_description: null }],
      },
    ],
    security_events: [],
    claim_set_version: 1,
    reviews: [],
    state_transitions: [
      { from_state: "CREATED", to_state: "PLANNING" },
      { from_state: "PLANNING", to_state: "GATHERING_EVIDENCE" },
    ],
    final_status: "PUBLISHED",
    model_calls: [
      { purpose: "draft_claim", provider_id: "test-double-external", model_id: "deterministic-echo-v1", latency_ms: 1, tokens_in: 10, tokens_out: 5, cost: 0 },
    ],
    total_latency_ms: 1,
    total_tokens: 15,
    total_cost: 0,
    created_by: "analyst_1",
    classification: "PUBLIC",
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:05:00+00:00",
    publication_allowed: false,
    publication_gate_reasons: ["task.state is PUBLISHED, not APPROVED"],
    stale_approval: false,
    ...overrides,
  };
}
