// Mirrors backend/src/afra/api/app.py's JSON responses exactly. This file
// has no logic - it is a type view over what the backend already computes.
// If a shape changes on the backend, it changes here too; nothing here is
// a second source of truth for state-machine, policy, or grading rules.

export type TaskState =
  | "CREATED"
  | "PLANNING"
  | "NEEDS_CLARIFICATION"
  | "GATHERING_EVIDENCE"
  | "SYNTHESISING"
  | "VALIDATING"
  | "INSUFFICIENT_EVIDENCE"
  | "SECURITY_BLOCKED"
  | "AWAITING_REVIEW"
  | "REVISION_REQUESTED"
  | "APPROVED"
  | "REJECTED"
  | "PUBLISHED"
  | "FAILED";

// The full documented state-machine path, in the order a task would pass
// through it on a "everything resolves cleanly" run - used only to render
// a timeline skeleton; which states a given task actually reached comes
// from state_transitions, never assumed from this list.
export const ALL_STATES: TaskState[] = [
  "CREATED",
  "PLANNING",
  "GATHERING_EVIDENCE",
  "SYNTHESISING",
  "VALIDATING",
  "AWAITING_REVIEW",
  "APPROVED",
  "PUBLISHED",
];

export interface TaskSummary {
  task_id: string;
  question_text: string;
  state: TaskState;
  created_by: string;
  classification: string;
  claim_set_version: number;
  created_at: string | null;
  updated_at: string | null;
  publication_allowed: boolean;
}

export interface ScopeField {
  status: "RESOLVED" | "AMBIGUOUS" | "MISSING";
  value: unknown;
  reason: string | null;
}

export interface ToolCall {
  tool_name: string;
  input_summary: string;
  output_summary: string;
  succeeded: boolean;
}

export interface RoutingDecision {
  subquestion: string;
  decision: string;
}

export interface EvidenceItem {
  evidence_id: string;
  source_document_id: string;
  source_location: string;
  classification: string;
}

export interface EvidenceLink {
  evidence_id: string;
  quote_span: string;
  support_contribution: string;
}

export interface ValidationResultView {
  validation_id: string;
  computed_support_status: string;
  citation_check_passed: boolean;
  missing_evidence_description: string | null;
}

export interface ClaimView {
  claim_id: string;
  claim_text: string;
  model_id: string;
  support_status: string | null;
  support_strength: number | null;
  approval_status: string;
  evidence_links: EvidenceLink[];
  validation_results: ValidationResultView[];
}

export interface SecurityEventView {
  trigger: string;
  classification: string;
  action_taken: "allow" | "redact" | "block" | "route_private" | "require_approval";
  detected_categories: string[];
  requested_provider_id: string | null;
  selected_provider_id: string | null;
  policy_result: string;
  resolved_by: string | null;
}

export interface ReviewView {
  reviewer_id: string;
  decision: "approve" | "request_revision" | "reject";
  comments: string;
  claim_set_version: number;
  security_context: string | null;
  reviewed_at: string | null;
}

export interface StateTransitionView {
  from_state: TaskState;
  to_state: TaskState;
}

export interface ModelCallView {
  purpose: string;
  provider_id: string;
  model_id: string;
  latency_ms: number;
  tokens_in: number;
  tokens_out: number;
  cost: number;
}

export interface TaskDetail {
  task_id: string;
  question_text: string;
  interpreted_scope: Record<string, ScopeField> | null;
  unresolved_fields: string | null;
  clarification_responses: unknown[];
  plan: string[];
  tool_calls: ToolCall[];
  routing_decisions: RoutingDecision[];
  sufficiency_result: Record<string, unknown> | null;
  abstention_reason: string | null;
  retrieved_evidence: EvidenceItem[];
  draft_claims: ClaimView[];
  security_events: SecurityEventView[];
  claim_set_version: number;
  reviews: ReviewView[];
  state_transitions: StateTransitionView[];
  final_status: TaskState;
  model_calls: ModelCallView[];
  total_latency_ms: number;
  total_tokens: number;
  total_cost: number;
  created_by: string;
  classification: string;
  created_at: string | null;
  updated_at: string | null;
  publication_allowed: boolean;
  publication_gate_reasons: string[];
  stale_approval: boolean;
}

export interface MetricSummary {
  value: number | null;
  n: number;
}

export interface ConfigurationAggregate {
  configuration: string;
  task_count: number;
  completion_rate: MetricSummary;
  retrieval_recall: MetricSummary;
  citation_precision: MetricSummary;
  unsupported_claim_rate: MetricSummary;
  abstention_accuracy: MetricSummary;
  clarification_accuracy: MetricSummary;
  prompt_injection_attack_success_rate: MetricSummary;
  policy_block_accuracy: MetricSummary;
  mean_model_call_count: number | null;
  mean_tool_call_count: number | null;
  mean_wall_clock_ms: number | null;
  total_cost: number;
  by_category: Record<string, unknown>;
}

export interface DeterministicBenchmark {
  run_id: string;
  manifest: Record<string, unknown>;
  configurations: Partial<Record<"A" | "D" | "F", ConfigurationAggregate>>;
  label: string;
}

export interface RealModelValidationStatus {
  status: "complete" | "pending" | string;
  label: string;
  run_id?: string;
  git_commit?: string;
  execution_backend?: string;
  underlying_model?: string;
  manifest?: {
    repeat_count?: number;
    incomplete_attempts?: number;
    rate_limited_attempts?: number;
    provider_error_attempts?: number;
  };
  configurations?: Partial<Record<"A" | "D" | "F", ConfigurationAggregate>>;
  frozen_subset: {
    version: string;
    categories: string[];
    task_ids: string[];
    rationale: string;
  } | null;
}

export interface EvaluationResponse {
  deterministic_benchmark: DeterministicBenchmark | null;
  real_model_validation: RealModelValidationStatus;
}

export type ReviewDecisionValue = "approve" | "request_revision" | "reject";
