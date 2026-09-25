import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { EvaluationResponse } from "../api/types";
import { Evaluation } from "./Evaluation";

vi.mock("../api/client", () => ({
  getEvaluation: vi.fn(),
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { getEvaluation } from "../api/client";

function makeResponse(overrides: Partial<EvaluationResponse> = {}): EvaluationResponse {
  return {
    deterministic_benchmark: {
      run_id: "run_f3bb065c08c9",
      manifest: {},
      configurations: {
        A: {
          configuration: "A", task_count: 50,
          completion_rate: { value: 1, n: 50 }, retrieval_recall: { value: null, n: 0 },
          citation_precision: { value: null, n: 0 }, unsupported_claim_rate: { value: 0.52, n: 42 },
          abstention_accuracy: { value: 0.74, n: 42 }, clarification_accuracy: { value: 0, n: 6 },
          prompt_injection_attack_success_rate: { value: 0.33, n: 6 }, policy_block_accuracy: { value: 0.6, n: 5 },
          mean_model_call_count: 1, mean_tool_call_count: 0, mean_wall_clock_ms: 0.08, total_cost: 0, by_category: {},
        },
        D: {
          configuration: "D", task_count: 45,
          completion_rate: { value: 1, n: 45 }, retrieval_recall: { value: 0.77, n: 39 },
          citation_precision: { value: 0.95, n: 39 }, unsupported_claim_rate: { value: 0.1556, n: 45 },
          abstention_accuracy: { value: 0.72, n: 39 }, clarification_accuracy: { value: 1, n: 6 },
          prompt_injection_attack_success_rate: { value: 0, n: 6 }, policy_block_accuracy: { value: null, n: 0 },
          mean_model_call_count: 3, mean_tool_call_count: 5.6, mean_wall_clock_ms: 6.5, total_cost: 0, by_category: {},
        },
        F: {
          configuration: "F", task_count: 50,
          completion_rate: { value: 1, n: 50 }, retrieval_recall: { value: 0.8, n: 44 },
          citation_precision: { value: 0.95, n: 42 }, unsupported_claim_rate: { value: 0, n: 50 },
          abstention_accuracy: { value: 1, n: 42 }, clarification_accuracy: { value: 1, n: 6 },
          prompt_injection_attack_success_rate: { value: 0, n: 6 }, policy_block_accuracy: { value: 1, n: 5 },
          mean_model_call_count: 2.8, mean_tool_call_count: 5.2, mean_wall_clock_ms: 7, total_cost: 0, by_category: {},
        },
      },
      label: "Deterministic benchmark - synthetic fixture corpus (8 fictional documents), test-double providers only. No real external LLM was used to produce these numbers.",
    },
    real_model_validation: {
      status: "complete",
      label: "Phase 6.5 canonical live validation complete on frozen synthetic subset (14 tasks, 7 categories, 3 repeats per configuration). Replicates qualitative A -> D -> F improvement from deterministic Phase 6.",
      run_id: "realmodel_live_1fb8b08dbbc3",
      git_commit: "4386ac6f247226ad297d02573acedabb5d00f0d8",
      execution_backend: "Antigravity CLI",
      underlying_model: "gemini-3.8-flash-low",
      manifest: {
        repeat_count: 3,
        incomplete_attempts: 0,
        rate_limited_attempts: 0,
        provider_error_attempts: 0,
      },
      configurations: {
        A: {
          configuration: "A", task_count: 14,
          completion_rate: { value: 1, n: 14 }, retrieval_recall: { value: null, n: 0 },
          citation_precision: { value: null, n: 0 }, unsupported_claim_rate: { value: 0.8181818182, n: 11 },
          abstention_accuracy: { value: 0.6363636364, n: 11 }, clarification_accuracy: { value: 0, n: 2 },
          prompt_injection_attack_success_rate: { value: 0, n: 2 }, policy_block_accuracy: { value: 0.5, n: 2 },
          mean_model_call_count: 1, mean_tool_call_count: 0, mean_wall_clock_ms: 0.24, total_cost: 0, by_category: {},
        },
        D: {
          configuration: "D", task_count: 14,
          completion_rate: { value: 1, n: 12 }, retrieval_recall: { value: 1, n: 10 },
          citation_precision: { value: 0.9333333333, n: 10 }, unsupported_claim_rate: { value: 0.2222222222, n: 12 },
          abstention_accuracy: { value: 0.6, n: 10 }, clarification_accuracy: { value: 1, n: 2 },
          prompt_injection_attack_success_rate: { value: 0, n: 2 }, policy_block_accuracy: { value: null, n: 0 },
          mean_model_call_count: 2.67, mean_tool_call_count: 5.17, mean_wall_clock_ms: 30279.74, total_cost: 0, by_category: {},
        },
        F: {
          configuration: "F", task_count: 14,
          completion_rate: { value: 1, n: 14 }, retrieval_recall: { value: 1, n: 12 },
          citation_precision: { value: 0.9393939394, n: 11 }, unsupported_claim_rate: { value: 0, n: 14 },
          abstention_accuracy: { value: 1, n: 11 }, clarification_accuracy: { value: 1, n: 2 },
          prompt_injection_attack_success_rate: { value: 0, n: 2 }, policy_block_accuracy: { value: 1, n: 2 },
          mean_model_call_count: 2.36, mean_tool_call_count: 4.57, mean_wall_clock_ms: 26206.44, total_cost: 0, by_category: {},
        },
      },
      frozen_subset: {
        version: "v1",
        categories: ["factual_retrieval", "multi_document_comparison", "ambiguous_clarification", "insufficient_evidence", "conflicting_evidence", "prompt_injection", "sensitive_data_routing_policy"],
        task_ids: Array.from({ length: 14 }, (_, i) => `T-${i}`),
        rationale: "stratified selection",
      },
    },
    ...overrides,
  };
}

describe("Evaluation page - real-model validation", () => {
  it("shows COMPLETE with canonical run ID and labels it a frozen synthetic subset", async () => {
    vi.mocked(getEvaluation).mockResolvedValue(makeResponse());

    render(<Evaluation />);

    await waitFor(() => expect(screen.getByText("COMPLETE.")).toBeInTheDocument());
    expect(screen.getByText(/realmodel_live_1fb8b08dbbc3/)).toBeInTheDocument();
    expect(screen.getByText(/4386ac6f2472/)).toBeInTheDocument();
    expect(screen.getAllByText(/frozen synthetic subset/i).length).toBeGreaterThan(0);
  });

  it("shows the frozen subset task count and category count from the real backend data", async () => {
    vi.mocked(getEvaluation).mockResolvedValue(makeResponse());

    render(<Evaluation />);

    await waitFor(() =>
      expect(
        screen.getByText(/frozen synthetic subset of 14 tasks across 7 categories/i),
      ).toBeInTheDocument(),
    );
  });

  it("labels the deterministic benchmark as synthetic and reports no real LLM was used", async () => {
    vi.mocked(getEvaluation).mockResolvedValue(makeResponse());

    render(<Evaluation />);

    await waitFor(() => expect(screen.getByText(/synthetic fixture corpus/)).toBeInTheDocument());
    expect(screen.getByText(/no real external llm/i)).toBeInTheDocument();
  });

  it("renders the current reproducible D unsupported-claim rate (not a stale hardcoded number)", async () => {
    vi.mocked(getEvaluation).mockResolvedValue(makeResponse());

    render(<Evaluation />);

    // 0.1556 -> "16%" at the table's whole-percent display
    await waitFor(() => expect(screen.getByText("16% (n=45)")).toBeInTheDocument());
  });

  it("handles a missing deterministic benchmark run gracefully", async () => {
    vi.mocked(getEvaluation).mockResolvedValue(makeResponse({ deterministic_benchmark: null }));

    render(<Evaluation />);

    await waitFor(() => expect(screen.getByText(/no benchmark run found on disk/i)).toBeInTheDocument());
  });
});
