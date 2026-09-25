import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, getTask, publishTask, resumeTask, submitReview } from "../api/client";
import type { ReviewDecisionValue, TaskDetail as TaskDetailType } from "../api/types";
import { ActionBadge, StatusBadge, SupportBadge } from "../components/StatusBadge";
import { StateTimeline } from "../components/StateTimeline";

export function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>();
  const [task, setTask] = useState<TaskDetailType | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(() => {
    if (!taskId) return;
    getTask(taskId)
      .then(setTask)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load task"));
  }, [taskId]);

  useEffect(() => {
    reload();
  }, [reload]);

  const runAction = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      setActionError(null);
      try {
        await fn();
        reload();
      } catch (err) {
        setActionError(err instanceof ApiError ? err.message : "Action failed");
      } finally {
        setBusy(false);
      }
    },
    [reload],
  );

  if (error) return <p className="error-text">{error}</p>;
  if (task === null) return <p className="loading">Loading task…</p>;

  return (
    <div className="stack">
      <Link to="/tasks" className="faint">
        &larr; All tasks
      </Link>

      <OverviewPanel task={task} />
      <div className="panel">
        <div className="panel-title">Workflow / State Timeline</div>
        <StateTimeline transitions={task.state_transitions} />
      </div>

      <PlanPanel task={task} />
      <EvidencePanel task={task} />
      <ClaimsPanel task={task} />
      <SecurityPanel task={task} />
      <ReviewPanel
        task={task}
        busy={busy}
        actionError={actionError}
        onSubmitReview={(reviewerId, decision, comments) =>
          runAction(() => submitReview(task.task_id, { reviewer_id: reviewerId, decision, comments }))
        }
        onPublish={() => runAction(() => publishTask(task.task_id))}
        onResume={(needsNewEvidence) =>
          runAction(() => resumeTask(task.task_id, { needs_new_evidence: needsNewEvidence }))
        }
      />
      <AuditTracePanel task={task} />
    </div>
  );
}

function OverviewPanel({ task }: { task: TaskDetailType }) {
  return (
    <div className="panel">
      <div className="panel-title">
        <span>Research Task Overview</span>
        <span className="mono faint">{task.task_id}</span>
      </div>
      <p style={{ fontSize: 15, fontWeight: 600, margin: "0 0 14px 0" }}>{task.question_text}</p>
      <div className="kv-grid">
        <div className="kv-item">
          <div className="kv-label">State</div>
          <div className="kv-value">
            <StatusBadge state={task.final_status} />
          </div>
        </div>
        <div className="kv-item">
          <div className="kv-label">Created by</div>
          <div className="kv-value mono">{task.created_by}</div>
        </div>
        <div className="kv-item">
          <div className="kv-label">Data classification</div>
          <div className="kv-value">
            <span className="pill">{task.classification}</span>
          </div>
        </div>
        <div className="kv-item">
          <div className="kv-label">Claim set version</div>
          <div className="kv-value">{task.claim_set_version}</div>
        </div>
        <div className="kv-item">
          <div className="kv-label">Publication currently allowed</div>
          <div className="kv-value">
            <span className={`badge badge-${task.publication_allowed ? "ok" : "danger"}`}>
              {task.publication_allowed ? "YES" : "NO"}
            </span>
          </div>
        </div>
        <div className="kv-item">
          <div className="kv-label">Stale approval</div>
          <div className="kv-value">
            <span className={`badge badge-${task.stale_approval ? "danger" : "neutral"}`}>
              {task.stale_approval ? "STALE" : "N/A"}
            </span>
          </div>
        </div>
        <div className="kv-item">
          <div className="kv-label">Created</div>
          <div className="kv-value faint">
            {task.created_at ? new Date(task.created_at).toLocaleString() : "—"}
          </div>
        </div>
        <div className="kv-item">
          <div className="kv-label">Last updated</div>
          <div className="kv-value faint">
            {task.updated_at ? new Date(task.updated_at).toLocaleString() : "—"}
          </div>
        </div>
      </div>

      {task.interpreted_scope && (
        <>
          <div className="panel-title" style={{ marginTop: 18 }}>
            Research Scope
          </div>
          <table className="dense">
            <thead>
              <tr>
                <th>Field</th>
                <th>Status</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(task.interpreted_scope).map(([field, scope]) => (
                <tr key={field}>
                  <td className="mono">{field}</td>
                  <td>
                    <span
                      className={`badge badge-${scope.status === "RESOLVED" ? "ok" : scope.status === "AMBIGUOUS" ? "warn" : "danger"}`}
                    >
                      {scope.status}
                    </span>
                  </td>
                  <td>{scope.value == null ? <span className="faint">—</span> : String(scope.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {task.unresolved_fields && (
        <div className="banner banner-warn" style={{ marginTop: 12 }}>
          Awaiting clarification: <strong>{task.unresolved_fields}</strong>
        </div>
      )}
      {task.abstention_reason && (
        <div className="banner banner-warn" style={{ marginTop: 12 }}>
          Abstention reason: {task.abstention_reason}
        </div>
      )}
      {!task.publication_allowed && task.publication_gate_reasons.length > 0 && (
        <div className="banner banner-warn" style={{ marginTop: 12 }}>
          <strong>Publication gate:</strong>
          <ul style={{ margin: "6px 0 0 18px", padding: 0 }}>
            {task.publication_gate_reasons.map((reason, i) => (
              <li key={i}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function PlanPanel({ task }: { task: TaskDetailType }) {
  return (
    <div className="panel">
      <div className="panel-title">Plan / Tool Activity</div>
      {task.plan.length === 0 ? (
        <p className="panel-empty">No plan generated (task stopped before planning completed).</p>
      ) : (
        <ol style={{ margin: "0 0 14px 18px", padding: 0, fontSize: 12.5 }}>
          {task.plan.map((subquestion, i) => (
            <li key={i}>{subquestion}</li>
          ))}
        </ol>
      )}

      {task.routing_decisions.length > 0 && (
        <>
          <div className="faint" style={{ marginBottom: 6, fontWeight: 600 }}>
            Routing decisions
          </div>
          <table className="dense" style={{ marginBottom: 14 }}>
            <thead>
              <tr>
                <th>Subquestion</th>
                <th>Decision</th>
              </tr>
            </thead>
            <tbody>
              {task.routing_decisions.map((rd, i) => (
                <tr key={i}>
                  <td>{rd.subquestion}</td>
                  <td className="mono">{rd.decision}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <div className="faint" style={{ marginBottom: 6, fontWeight: 600 }}>
        Tool calls ({task.tool_calls.length})
      </div>
      {task.tool_calls.length === 0 ? (
        <p className="panel-empty">No tool calls recorded.</p>
      ) : (
        <table className="dense">
          <thead>
            <tr>
              <th>Tool</th>
              <th>Input</th>
              <th>Result</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {task.tool_calls.map((tc, i) => (
              <tr key={i}>
                <td className="mono">{tc.tool_name}</td>
                <td className="mono">{tc.input_summary}</td>
                <td className="mono">{tc.output_summary}</td>
                <td>
                  <span className={`badge badge-${tc.succeeded ? "ok" : "danger"}`}>
                    {tc.succeeded ? "ok" : "failed"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function EvidencePanel({ task }: { task: TaskDetailType }) {
  const usedEvidenceIds = new Set(task.draft_claims.flatMap((c) => c.evidence_links.map((l) => l.evidence_id)));
  return (
    <div className="panel">
      <div className="panel-title">Evidence ({task.retrieved_evidence.length})</div>
      {task.retrieved_evidence.length === 0 ? (
        <p className="panel-empty">No evidence retrieved.</p>
      ) : (
        <table className="dense">
          <thead>
            <tr>
              <th>Evidence ID</th>
              <th>Source document</th>
              <th>Section</th>
              <th>Classification</th>
              <th>Used by a claim</th>
            </tr>
          </thead>
          <tbody>
            {task.retrieved_evidence.map((ev) => (
              <tr key={ev.evidence_id}>
                <td className="mono">{ev.evidence_id}</td>
                <td className="mono">{ev.source_document_id}</td>
                <td className="mono">{ev.source_location}</td>
                <td>
                  <span className="pill">{ev.classification}</span>
                </td>
                <td>
                  <span className={`badge badge-${usedEvidenceIds.has(ev.evidence_id) ? "ok" : "neutral"}`}>
                    {usedEvidenceIds.has(ev.evidence_id) ? "yes" : "no"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ClaimsPanel({ task }: { task: TaskDetailType }) {
  return (
    <div className="panel">
      <div className="panel-title">Claims &amp; Validation ({task.draft_claims.length})</div>
      {task.draft_claims.length === 0 ? (
        <p className="panel-empty">No claims drafted.</p>
      ) : (
        <div className="stack">
          {task.draft_claims.map((claim) => {
            const latestValidation = claim.validation_results[claim.validation_results.length - 1];
            return (
              <div key={claim.claim_id} style={{ borderTop: "1px solid var(--border)", paddingTop: 10 }}>
                <div className="form-row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
                  <SupportBadge status={claim.support_status} />
                  <span className="faint mono">{claim.claim_id}</span>
                </div>
                <p style={{ margin: "4px 0" }}>{claim.claim_text}</p>
                <div className="faint" style={{ marginBottom: 4 }}>
                  model: {claim.model_id} · approval: {claim.approval_status}
                  {claim.support_strength != null && <> · strength: {claim.support_strength.toFixed(2)}</>}
                </div>
                {claim.evidence_links.map((link, i) => (
                  <div key={i} className="quote">
                    “{link.quote_span}” <span className="faint">({link.support_contribution}, {link.evidence_id})</span>
                  </div>
                ))}
                {latestValidation && (
                  <div className="faint">
                    citation check:{" "}
                    <span className={`badge badge-${latestValidation.citation_check_passed ? "ok" : "danger"}`}>
                      {latestValidation.citation_check_passed ? "passed" : "failed"}
                    </span>
                    {latestValidation.missing_evidence_description && (
                      <> · {latestValidation.missing_evidence_description}</>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SecurityPanel({ task }: { task: TaskDetailType }) {
  return (
    <div className="panel">
      <div className="panel-title">Security / Policy</div>
      <p className="faint" style={{ marginTop: 0 }}>
        Prompt-injection pattern detection is an audit signal only (visible in "detected categories"
        below) - the actual defence is structural: providers never treat retrieved document text as
        instructions, and every model call is routed through the classification/DLP policy below
        before it happens, independent of anything a document contains. This is a six-pattern
        deterministic regex scanner, not enterprise DLP - see docs/DATA_CLASSIFICATION.md.
      </p>
      {task.security_events.length === 0 ? (
        <p className="panel-empty">No security/policy events recorded for this task.</p>
      ) : (
        <table className="dense">
          <thead>
            <tr>
              <th>Trigger</th>
              <th>Classification</th>
              <th>Decision</th>
              <th>Detected categories</th>
              <th>Requested provider</th>
              <th>Selected provider</th>
              <th>Reason</th>
              <th>Resolved by</th>
            </tr>
          </thead>
          <tbody>
            {task.security_events.map((se, i) => (
              <tr key={i}>
                <td className="mono">{se.trigger}</td>
                <td>
                  <span className="pill">{se.classification}</span>
                </td>
                <td>
                  <ActionBadge action={se.action_taken} />
                </td>
                <td className="faint">
                  {se.detected_categories.length > 0 ? se.detected_categories.join(", ") : "—"}
                </td>
                <td className="mono">{se.requested_provider_id ?? "—"}</td>
                <td className="mono">{se.selected_provider_id ?? "—"}</td>
                <td className="faint">{se.policy_result}</td>
                <td>{se.resolved_by ?? <span className="faint">unresolved</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ReviewPanel({
  task,
  busy,
  actionError,
  onSubmitReview,
  onPublish,
  onResume,
}: {
  task: TaskDetailType;
  busy: boolean;
  actionError: string | null;
  onSubmitReview: (reviewerId: string, decision: ReviewDecisionValue, comments: string) => void;
  onPublish: () => void;
  onResume: (needsNewEvidence: boolean) => void;
}) {
  const [reviewerId, setReviewerId] = useState("reviewer_1");
  const [decision, setDecision] = useState<ReviewDecisionValue>("approve");
  const [comments, setComments] = useState("");

  return (
    <div className="panel">
      <div className="panel-title">Human Review</div>

      {task.reviews.length === 0 ? (
        <p className="panel-empty">No review decisions recorded yet.</p>
      ) : (
        <table className="dense" style={{ marginBottom: 14 }}>
          <thead>
            <tr>
              <th>Reviewer</th>
              <th>Decision</th>
              <th>Version reviewed</th>
              <th>Notes</th>
              <th>Reviewed at</th>
            </tr>
          </thead>
          <tbody>
            {task.reviews.map((review, i) => {
              const isStale = review.decision === "approve" && review.claim_set_version !== task.claim_set_version;
              return (
                <tr key={i}>
                  <td className="mono">{review.reviewer_id}</td>
                  <td>
                    <span
                      className={`badge badge-${review.decision === "approve" ? "ok" : review.decision === "reject" ? "danger" : "warn"}`}
                    >
                      {review.decision.replace(/_/g, " ")}
                    </span>
                    {isStale && <span className="badge badge-danger" style={{ marginLeft: 6 }}>stale</span>}
                  </td>
                  <td>{review.claim_set_version}</td>
                  <td className="faint">{review.comments || "—"}</td>
                  <td className="faint">
                    {review.reviewed_at ? new Date(review.reviewed_at).toLocaleString() : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {actionError && <p className="error-text">{actionError}</p>}

      {task.final_status === "AWAITING_REVIEW" && (
        <div className="form-row">
          <select value={reviewerId} onChange={(e) => setReviewerId(e.target.value)}>
            <option value="reviewer_1">reviewer_1</option>
            <option value="governance_admin">governance_admin</option>
          </select>
          <select value={decision} onChange={(e) => setDecision(e.target.value as ReviewDecisionValue)}>
            <option value="approve">Approve</option>
            <option value="request_revision">Request revision</option>
            <option value="reject">Reject</option>
          </select>
          <input
            type="text"
            placeholder="Comments (optional)"
            value={comments}
            onChange={(e) => setComments(e.target.value)}
            style={{ minWidth: 220 }}
          />
          <button
            className="button primary"
            disabled={busy}
            onClick={() => onSubmitReview(reviewerId, decision, comments)}
          >
            Submit decision
          </button>
        </div>
      )}

      {task.final_status === "APPROVED" && (
        <div className="form-row">
          <button className="button primary" disabled={busy || !task.publication_allowed} onClick={onPublish}>
            Publish
          </button>
          {!task.publication_allowed && (
            <span className="faint">Blocked by the publication gate - see reasons above.</span>
          )}
        </div>
      )}

      {task.final_status === "REVISION_REQUESTED" && (
        <div className="form-row">
          <button className="button" disabled={busy} onClick={() => onResume(false)}>
            Resume (wording/drafting only)
          </button>
          <button className="button" disabled={busy} onClick={() => onResume(true)}>
            Resume (needs new evidence)
          </button>
        </div>
      )}
    </div>
  );
}

function AuditTracePanel({ task }: { task: TaskDetailType }) {
  type Row = { at: string | null; component: string; event: string; state: string; objectId: string };
  const rows: Row[] = [];

  task.state_transitions.forEach((t) =>
    rows.push({ at: null, component: "orchestrator", event: `${t.from_state} → ${t.to_state}`, state: t.to_state, objectId: task.task_id }),
  );
  task.tool_calls.forEach((tc) =>
    rows.push({ at: null, component: "tool layer", event: `${tc.tool_name}: ${tc.output_summary}`, state: "—", objectId: tc.tool_name }),
  );
  task.model_calls.forEach((mc) =>
    rows.push({
      at: null,
      component: "model provider",
      event: `${mc.purpose} via ${mc.provider_id} (${mc.model_id}), ${mc.tokens_in + mc.tokens_out} tokens`,
      state: "—",
      objectId: mc.provider_id,
    }),
  );
  task.security_events.forEach((se) =>
    rows.push({ at: null, component: "policy enforcement", event: `${se.trigger}: ${se.action_taken}`, state: se.classification, objectId: se.selected_provider_id ?? se.requested_provider_id ?? "—" }),
  );
  task.reviews.forEach((r) =>
    rows.push({ at: r.reviewed_at, component: "human review", event: `${r.reviewer_id}: ${r.decision}`, state: `v${r.claim_set_version}`, objectId: r.reviewer_id }),
  );

  return (
    <div className="panel">
      <div className="panel-title">Audit Trace</div>
      <p className="faint" style={{ marginTop: 0 }}>
        Every row below is a persisted record ({task.total_tokens} tokens · ${task.total_cost.toFixed(4)}{" "}
        total cost across {task.model_calls.length} model call(s), {task.total_latency_ms}ms total
        model latency) - not reconstructed or inferred client-side.
      </p>
      {rows.length === 0 ? (
        <p className="panel-empty">No audit events recorded.</p>
      ) : (
        <table className="dense">
          <thead>
            <tr>
              <th>Component</th>
              <th>Event / action</th>
              <th>Context</th>
              <th>Object</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                <td className="mono">{row.component}</td>
                <td>{row.event}</td>
                <td className="faint">{row.state}</td>
                <td className="mono faint">{row.objectId}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
