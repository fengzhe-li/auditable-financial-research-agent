import type {
  EvaluationResponse,
  ReviewDecisionValue,
  TaskDetail,
  TaskSummary,
} from "./types";

// In dev, Vite proxies /api to the FastAPI backend (see vite.config.ts) -
// same path works unchanged if the built frontend is ever served from the
// same origin as the API. No other base URL configuration exists here on
// purpose: this console talks to exactly one backend.
const API_BASE = "/api";

class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      // response body wasn't JSON - keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export function listTasks(): Promise<{ tasks: TaskSummary[] }> {
  return request("/tasks");
}

export function getTask(taskId: string): Promise<TaskDetail> {
  return request(`/tasks/${encodeURIComponent(taskId)}`);
}

export function submitReview(
  taskId: string,
  body: { reviewer_id: string; decision: ReviewDecisionValue; comments?: string },
): Promise<{ task_id: string; state: string }> {
  return request(`/tasks/${encodeURIComponent(taskId)}/review`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function publishTask(taskId: string): Promise<{ task_id: string; state: string }> {
  return request(`/tasks/${encodeURIComponent(taskId)}/publish`, { method: "POST" });
}

export function resumeTask(
  taskId: string,
  body: { needs_new_evidence?: boolean } = {},
): Promise<{ task_id: string; state: string }> {
  return request(`/tasks/${encodeURIComponent(taskId)}/resume`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getEvaluation(): Promise<EvaluationResponse> {
  return request("/evaluation");
}

export { ApiError };
