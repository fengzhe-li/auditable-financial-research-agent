import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { makeTaskDetail } from "../test/fixtures";
import { TaskDetail } from "./TaskDetail";

vi.mock("../api/client", () => ({
  getTask: vi.fn(),
  submitReview: vi.fn(),
  publishTask: vi.fn(),
  resumeTask: vi.fn(),
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { getTask } from "../api/client";

function renderTask(taskId = "task_abc123") {
  return render(
    <MemoryRouter initialEntries={[`/tasks/${taskId}`]}>
      <Routes>
        <Route path="/tasks/:taskId" element={<TaskDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("TaskDetail - publication safety", () => {
  it("a SECURITY_BLOCKED task is never shown as publishable and has no Publish button", async () => {
    vi.mocked(getTask).mockResolvedValue(
      makeTaskDetail({
        final_status: "SECURITY_BLOCKED",
        publication_allowed: false,
        publication_gate_reasons: ["task.state is SECURITY_BLOCKED, not APPROVED"],
        state_transitions: [
          { from_state: "CREATED", to_state: "PLANNING" },
          { from_state: "PLANNING", to_state: "SECURITY_BLOCKED" },
        ],
      }),
    );

    renderTask();

    await waitFor(() => expect(screen.getAllByText("SECURITY BLOCKED").length).toBeGreaterThan(0));
    expect(screen.getByText("NO")).toBeInTheDocument(); // publication currently allowed
    expect(screen.queryByRole("button", { name: /^publish$/i })).not.toBeInTheDocument();
  });

  it("an INSUFFICIENT_EVIDENCE task is never shown as publishable", async () => {
    vi.mocked(getTask).mockResolvedValue(
      makeTaskDetail({
        final_status: "INSUFFICIENT_EVIDENCE",
        publication_allowed: false,
        publication_gate_reasons: ["task.state is INSUFFICIENT_EVIDENCE, not APPROVED"],
      }),
    );

    renderTask();

    await waitFor(() => expect(screen.getByText("NO")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^publish$/i })).not.toBeInTheDocument();
  });

  it("an APPROVED, non-stale task shows an enabled Publish button", async () => {
    vi.mocked(getTask).mockResolvedValue(
      makeTaskDetail({
        final_status: "APPROVED",
        publication_allowed: true,
        publication_gate_reasons: [],
        stale_approval: false,
        reviews: [
          { reviewer_id: "reviewer_1", decision: "approve", comments: "", claim_set_version: 1, security_context: null, reviewed_at: "2026-01-01T00:00:00+00:00" },
        ],
      }),
    );

    renderTask();

    const publishButton = await screen.findByRole("button", { name: /^publish$/i });
    expect(publishButton).not.toBeDisabled();
  });
});

describe("TaskDetail - stale approvals", () => {
  it("displays a stale approval clearly, with the gate reason and a disabled/blocked Publish state", async () => {
    vi.mocked(getTask).mockResolvedValue(
      makeTaskDetail({
        final_status: "APPROVED",
        claim_set_version: 2,
        publication_allowed: false,
        stale_approval: true,
        publication_gate_reasons: [
          "approval was recorded for claim_set_version=1, but the task's current claim_set_version is 2 - stale approval",
        ],
        reviews: [
          { reviewer_id: "reviewer_1", decision: "approve", comments: "", claim_set_version: 1, security_context: null, reviewed_at: "2026-01-01T00:00:00+00:00" },
        ],
      }),
    );

    renderTask();

    await waitFor(() => expect(screen.getByText("STALE")).toBeInTheDocument());
    expect(screen.getAllByText(/stale approval/i).length).toBeGreaterThan(0);
    const publishButton = screen.getByRole("button", { name: /^publish$/i });
    expect(publishButton).toBeDisabled();
  });

  it("does not flag a fresh, non-stale approval as stale", async () => {
    vi.mocked(getTask).mockResolvedValue(
      makeTaskDetail({
        final_status: "APPROVED",
        claim_set_version: 1,
        publication_allowed: true,
        stale_approval: false,
        reviews: [
          { reviewer_id: "reviewer_1", decision: "approve", comments: "", claim_set_version: 1, security_context: null, reviewed_at: "2026-01-01T00:00:00+00:00" },
        ],
      }),
    );

    renderTask();

    await waitFor(() => expect(screen.getByText("N/A")).toBeInTheDocument());
    expect(screen.queryByText("STALE")).not.toBeInTheDocument();
  });
});

describe("TaskDetail - revision / version history", () => {
  it("shows resume actions for a REVISION_REQUESTED task and the reviewed version", async () => {
    vi.mocked(getTask).mockResolvedValue(
      makeTaskDetail({
        final_status: "REVISION_REQUESTED",
        claim_set_version: 1,
        reviews: [
          { reviewer_id: "reviewer_1", decision: "request_revision", comments: "add more evidence", claim_set_version: 1, security_context: null, reviewed_at: "2026-01-01T00:00:00+00:00" },
        ],
      }),
    );

    renderTask();

    // "request revision" (lowercase - ReviewDecision's raw value) is the
    // review-decision badge, distinct from the (uppercase) "REVISION
    // REQUESTED" TaskState badge shown elsewhere on the same page.
    await waitFor(() => expect(screen.getByText("request revision")).toBeInTheDocument());
    expect(screen.getByText("add more evidence")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /wording\/drafting only/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /needs new evidence/i })).toBeInTheDocument();
  });

  it("shows multiple reviews across a revision cycle as distinct version-history rows", async () => {
    vi.mocked(getTask).mockResolvedValue(
      makeTaskDetail({
        final_status: "APPROVED",
        claim_set_version: 2,
        publication_allowed: true,
        stale_approval: false,
        reviews: [
          { reviewer_id: "reviewer_1", decision: "request_revision", comments: "v1 needs work", claim_set_version: 1, security_context: null, reviewed_at: "2026-01-01T00:00:00+00:00" },
          { reviewer_id: "reviewer_1", decision: "approve", comments: "v2 looks good", claim_set_version: 2, security_context: null, reviewed_at: "2026-01-02T00:00:00+00:00" },
        ],
      }),
    );

    renderTask();

    await waitFor(() => expect(screen.getByText("v1 needs work")).toBeInTheDocument());
    expect(screen.getByText("v2 looks good")).toBeInTheDocument();
  });
});

describe("TaskDetail - never a fake happy-path timeline", () => {
  it("only renders states the task actually reached", async () => {
    vi.mocked(getTask).mockResolvedValue(
      makeTaskDetail({
        final_status: "NEEDS_CLARIFICATION",
        state_transitions: [{ from_state: "CREATED", to_state: "PLANNING" }, { from_state: "PLANNING", to_state: "NEEDS_CLARIFICATION" }],
      }),
    );

    renderTask();

    await waitFor(() => expect(screen.getAllByText("NEEDS CLARIFICATION").length).toBeGreaterThan(0));
    expect(screen.queryByText("PUBLISHED")).not.toBeInTheDocument();
  });
});
