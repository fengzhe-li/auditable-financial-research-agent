import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StateTimeline } from "./StateTimeline";

describe("StateTimeline", () => {
  it("shows an empty message when no transitions are recorded", () => {
    render(<StateTimeline transitions={[]} />);
    expect(screen.getByText(/no state transitions recorded/i)).toBeInTheDocument();
  });

  it("renders only the states actually reached, not a fake happy path", () => {
    render(
      <StateTimeline
        transitions={[
          { from_state: "CREATED", to_state: "PLANNING" },
          { from_state: "PLANNING", to_state: "NEEDS_CLARIFICATION" },
        ]}
      />,
    );
    expect(screen.getByText("CREATED")).toBeInTheDocument();
    expect(screen.getByText("PLANNING")).toBeInTheDocument();
    expect(screen.getByText("NEEDS CLARIFICATION")).toBeInTheDocument();
    // Never reached in this task - must not appear, even though it's part
    // of the documented happy-path state machine.
    expect(screen.queryByText("PUBLISHED")).not.toBeInTheDocument();
    expect(screen.queryByText("AWAITING REVIEW")).not.toBeInTheDocument();
  });

  it("marks the last state as the final/highlighted node", () => {
    render(
      <StateTimeline
        transitions={[{ from_state: "CREATED", to_state: "SECURITY_BLOCKED" }]}
      />,
    );
    const finalNode = screen.getByText("SECURITY BLOCKED");
    expect(finalNode).toHaveClass("final");
  });
});
