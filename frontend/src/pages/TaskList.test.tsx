import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { makeTaskSummary } from "../test/fixtures";
import { TaskList } from "./TaskList";

vi.mock("../api/client", () => ({
  listTasks: vi.fn(),
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { listTasks } from "../api/client";

describe("TaskList", () => {
  it("renders every returned task with its status badge", async () => {
    vi.mocked(listTasks).mockResolvedValue({
      tasks: [
        makeTaskSummary({ task_id: "t1", state: "PUBLISHED", question_text: "Q1" }),
        makeTaskSummary({ task_id: "t2", state: "SECURITY_BLOCKED", question_text: "Q2" }),
      ],
    });

    render(
      <MemoryRouter>
        <TaskList />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText("Q1")).toBeInTheDocument());
    expect(screen.getByText("Q2")).toBeInTheDocument();
    expect(screen.getByText("PUBLISHED")).toBeInTheDocument();
    expect(screen.getByText("SECURITY BLOCKED")).toBeInTheDocument();
  });

  it("shows an empty-state message with the seed command when there are no tasks", async () => {
    vi.mocked(listTasks).mockResolvedValue({ tasks: [] });

    render(
      <MemoryRouter>
        <TaskList />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText(/no tasks yet/i)).toBeInTheDocument());
    expect(screen.getByText(/seed_demo_data\.py/)).toBeInTheDocument();
  });

  it("shows an error message if the API call fails", async () => {
    const { ApiError } = await import("../api/client");
    vi.mocked(listTasks).mockRejectedValue(new ApiError(500, "server exploded"));

    render(
      <MemoryRouter>
        <TaskList />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText("server exploded")).toBeInTheDocument());
  });
});
