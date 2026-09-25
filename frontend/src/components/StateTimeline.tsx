import type { StateTransitionView, TaskState } from "../api/types";

/**
 * Renders the actual persisted state-transition sequence for a task
 * (task.state_transitions, from afra.trace.assemble_trace()) - never a
 * fixed "happy path" list. A task that stopped at NEEDS_CLARIFICATION
 * shows exactly CREATED -> PLANNING -> NEEDS_CLARIFICATION, nothing beyond
 * that, because nothing beyond that happened.
 */
export function StateTimeline({ transitions }: { transitions: StateTransitionView[] }) {
  if (transitions.length === 0) {
    return <p className="panel-empty">No state transitions recorded yet.</p>;
  }

  const states: TaskState[] = [transitions[0].from_state, ...transitions.map((t) => t.to_state)];

  return (
    <div className="timeline">
      {states.map((state, index) => {
        const isFinal = index === states.length - 1;
        return (
          <span className="timeline-step" key={`${state}-${index}`}>
            {index > 0 && <span className="timeline-arrow">&rarr;</span>}
            <span className={`timeline-node reached${isFinal ? " final" : ""}`}>
              {state.replace(/_/g, " ")}
            </span>
          </span>
        );
      })}
    </div>
  );
}
