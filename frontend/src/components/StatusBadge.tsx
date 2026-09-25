import type { TaskState } from "../api/types";

// Classifies every documented TaskState (docs/STATE_MACHINE.md) into one of
// four visual categories. This is presentation only - it does not decide
// what state a task is in or what transitions are legal; it only chooses a
// color for a state the backend already reports.
const CATEGORY: Record<TaskState, "ok" | "warn" | "danger" | "neutral" | "accent"> = {
  CREATED: "neutral",
  PLANNING: "neutral",
  GATHERING_EVIDENCE: "neutral",
  SYNTHESISING: "neutral",
  VALIDATING: "neutral",
  NEEDS_CLARIFICATION: "warn",
  INSUFFICIENT_EVIDENCE: "warn",
  AWAITING_REVIEW: "accent",
  REVISION_REQUESTED: "warn",
  APPROVED: "ok",
  REJECTED: "danger",
  SECURITY_BLOCKED: "danger",
  PUBLISHED: "ok",
  FAILED: "danger",
};

export function StatusBadge({ state }: { state: TaskState }) {
  const category = CATEGORY[state] ?? "neutral";
  return <span className={`badge badge-${category}`}>{state.replace(/_/g, " ")}</span>;
}

export function ActionBadge({ action }: { action: string }) {
  const category =
    action === "allow"
      ? "ok"
      : action === "redact" || action === "route_private"
        ? "warn"
        : action === "block" || action === "require_approval"
          ? "danger"
          : "neutral";
  return <span className={`badge badge-${category}`}>{action.replace(/_/g, " ")}</span>;
}

export function SupportBadge({ status }: { status: string | null }) {
  if (!status) return <span className="badge badge-neutral">UNVALIDATED</span>;
  const category =
    status === "SUPPORTED"
      ? "ok"
      : status === "UNSUPPORTED" || status === "CONFLICTING_EVIDENCE"
        ? "danger"
        : "warn";
  return <span className={`badge badge-${category}`}>{status.replace(/_/g, " ")}</span>;
}
