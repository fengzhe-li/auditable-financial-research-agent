import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ActionBadge, StatusBadge, SupportBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it("renders every documented TaskState without crashing", () => {
    const states = [
      "CREATED", "PLANNING", "NEEDS_CLARIFICATION", "GATHERING_EVIDENCE",
      "SYNTHESISING", "VALIDATING", "INSUFFICIENT_EVIDENCE", "SECURITY_BLOCKED",
      "AWAITING_REVIEW", "REVISION_REQUESTED", "APPROVED", "REJECTED",
      "PUBLISHED", "FAILED",
    ] as const;
    for (const state of states) {
      const { unmount } = render(<StatusBadge state={state} />);
      expect(screen.getByText(state.replace(/_/g, " "))).toBeInTheDocument();
      unmount();
    }
  });

  it("renders SECURITY_BLOCKED as a danger badge", () => {
    render(<StatusBadge state="SECURITY_BLOCKED" />);
    expect(screen.getByText("SECURITY BLOCKED")).toHaveClass("badge-danger");
  });

  it("renders PUBLISHED as an ok badge", () => {
    render(<StatusBadge state="PUBLISHED" />);
    expect(screen.getByText("PUBLISHED")).toHaveClass("badge-ok");
  });
});

describe("ActionBadge", () => {
  it("renders block as danger", () => {
    render(<ActionBadge action="block" />);
    expect(screen.getByText("block")).toHaveClass("badge-danger");
  });

  it("renders allow as ok", () => {
    render(<ActionBadge action="allow" />);
    expect(screen.getByText("allow")).toHaveClass("badge-ok");
  });
});

describe("SupportBadge", () => {
  it("renders null status as UNVALIDATED", () => {
    render(<SupportBadge status={null} />);
    expect(screen.getByText("UNVALIDATED")).toBeInTheDocument();
  });

  it("renders UNSUPPORTED as danger", () => {
    render(<SupportBadge status="UNSUPPORTED" />);
    expect(screen.getByText("UNSUPPORTED")).toHaveClass("badge-danger");
  });

  it("renders CONFLICTING_EVIDENCE as danger", () => {
    render(<SupportBadge status="CONFLICTING_EVIDENCE" />);
    expect(screen.getByText("CONFLICTING EVIDENCE")).toHaveClass("badge-danger");
  });
});
