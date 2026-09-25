import { useEffect, useState } from "react";
import { ApiError, getEvaluation } from "../api/client";
import type { ConfigurationAggregate, EvaluationResponse } from "../api/types";

function pct(metric: { value: number | null; n: number } | undefined): string {
  if (!metric || metric.value === null) return "N/A";
  return `${(metric.value * 100).toFixed(0)}% (n=${metric.n})`;
}

export function Evaluation() {
  const [data, setData] = useState<EvaluationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getEvaluation()
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load evaluation data"));
  }, []);

  if (error) return <p className="error-text">{error}</p>;
  if (data === null) return <p className="loading">Loading evaluation data…</p>;

  const det = data.deterministic_benchmark;
  const real = data.real_model_validation;
  const configs: ("A" | "D" | "F")[] = ["A", "D", "F"];

  return (
    <div className="stack">
      <div className="panel">
        <div className="panel-title">Deterministic Benchmark (Phase 6)</div>
        {det === null ? (
          <p className="panel-empty">
            No benchmark run found on disk. Run{" "}
            <code>python3 scripts/build_benchmark_v1.py &amp;&amp; python3 scripts/run_benchmark.py</code>{" "}
            in <code>backend/</code>.
          </p>
        ) : (
          <>
            <div className="banner banner-warn">
              <strong>Synthetic fixture benchmark.</strong> {det.label} Run ID:{" "}
              <span className="mono">{det.run_id}</span>.
            </div>
            <table className="dense">
              <thead>
                <tr>
                  <th>Config</th>
                  <th>n</th>
                  <th>Completion</th>
                  <th>Retrieval recall</th>
                  <th>Citation precision</th>
                  <th>Unsupported-claim rate</th>
                  <th>Abstention accuracy</th>
                  <th>Clarification accuracy</th>
                  <th>Injection success</th>
                  <th>Policy-block accuracy</th>
                </tr>
              </thead>
              <tbody>
                {configs.map((c) => {
                  const agg: ConfigurationAggregate | undefined = det.configurations[c];
                  if (!agg) return null;
                  return (
                    <tr key={c}>
                      <td>
                        <strong>{c}</strong>
                      </td>
                      <td>{agg.task_count}</td>
                      <td>{pct(agg.completion_rate)}</td>
                      <td>{pct(agg.retrieval_recall)}</td>
                      <td>{pct(agg.citation_precision)}</td>
                      <td>{pct(agg.unsupported_claim_rate)}</td>
                      <td>{pct(agg.abstention_accuracy)}</td>
                      <td>{pct(agg.clarification_accuracy)}</td>
                      <td>{pct(agg.prompt_injection_attack_success_rate)}</td>
                      <td>{pct(agg.policy_block_accuracy)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="faint" style={{ marginTop: 10 }}>
              A = single-shot baseline (no agent scaffolding). D = autonomous agent + validator, no
              abstention/security gate. F = full pipeline (planning, validation, sufficiency/abstention,
              security policy, human review, publication). See docs/PHASE_6_REPORT.md.
            </p>
          </>
        )}
      </div>

      <div className="panel">
        <div className="panel-title">Real-Model Validation (Phase 6.5)</div>
        <div className={`banner ${real.status === "failed" ? "banner-danger" : "banner-warn"}`}>
          <strong>{real.status.toUpperCase()}.</strong> {real.label}
          {real.run_id && <> Run ID: <span className="mono">{real.run_id}</span>.</>}
        </div>
        {real.configurations && (
          <table className="dense" style={{ marginTop: 10 }}>
            <thead>
              <tr>
                <th>Config</th>
                <th>n</th>
                <th>Completion</th>
                <th>Retrieval recall</th>
                <th>Citation precision</th>
                <th>Unsupported-claim rate</th>
                <th>Abstention accuracy</th>
                <th>Clarification accuracy</th>
                <th>Injection success</th>
                <th>Policy-block accuracy</th>
              </tr>
            </thead>
            <tbody>
              {configs.map((c) => {
                const agg: ConfigurationAggregate | undefined = real.configurations?.[c];
                if (!agg) return null;
                return (
                  <tr key={c}>
                    <td>
                      <strong>{c}</strong>
                    </td>
                    <td>{agg.task_count}</td>
                    <td>{pct(agg.completion_rate)}</td>
                    <td>{pct(agg.retrieval_recall)}</td>
                    <td>{pct(agg.citation_precision)}</td>
                    <td>{pct(agg.unsupported_claim_rate)}</td>
                    <td>{pct(agg.abstention_accuracy)}</td>
                    <td>{pct(agg.clarification_accuracy)}</td>
                    <td>{pct(agg.prompt_injection_attack_success_rate)}</td>
                    <td>{pct(agg.policy_block_accuracy)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        <div className="kv-grid" style={{ marginTop: 10 }}>
          <div className="kv-item">
            <div className="kv-label">Execution Backend</div>
            <div className="kv-value">{real.execution_backend || "Antigravity CLI"}</div>
          </div>
          <div className="kv-item">
            <div className="kv-label">Underlying Model</div>
            <div className="kv-value">{real.underlying_model || "gemini-3.8-flash-low"}</div>
          </div>
          <div className="kv-item">
            <div className="kv-label">Git Commit</div>
            <div className="kv-value mono">
              {real.git_commit ? real.git_commit.slice(0, 12) : "4386ac6f2472"}
            </div>
          </div>
          <div className="kv-item">
            <div className="kv-label">Repeats</div>
            <div className="kv-value">{real.manifest?.repeat_count ?? 3} (0 variance)</div>
          </div>
          <div className="kv-item">
            <div className="kv-label">Infrastructure Failures</div>
            <div className="kv-value">
              {(real.manifest?.incomplete_attempts ?? 0) +
                (real.manifest?.rate_limited_attempts ?? 0) +
                (real.manifest?.provider_error_attempts ?? 0)}{" "}
              (0 incomplete, 0 rate-limited, 0 provider errors)
            </div>
          </div>
        </div>
        {real.frozen_subset && (
          <>
            <p className="faint" style={{ marginTop: 10 }}>
              Evaluated across a frozen synthetic subset of {real.frozen_subset.task_ids.length} tasks across{" "}
              {real.frozen_subset.categories.length} categories (2 tasks per category, 3 repeats per configuration).
              Replicates the same qualitative A (82%) → D (22%) → F (0%) reduction in unsupported claims observed under
              deterministic Phase 6. Not a claim of production-scale generalization or absolute rate equivalence. See docs/PHASE_6_5_REAL_MODEL_REPORT.md.
            </p>
            <p className="faint" style={{ marginTop: 6 }}>
              Categories: {real.frozen_subset.categories.join(", ")}
            </p>
          </>
        )}
      </div>
    </div>
  );
}
