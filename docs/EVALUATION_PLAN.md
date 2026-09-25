# Evaluation Plan

**Phase 6 implemented this plan for real - see [docs/PHASE_6_REPORT.md](PHASE_6_REPORT.md) for the actual benchmark (`backend/benchmark/tasks_v1.json`, 50 tasks), the harness (`backend/afra/benchmark/`), and real results from a real, reproducible run.** This document remains the *design* specification below (kept largely as originally written, for the historical record of what was planned - see the Phase 6 deviations in [docs/ROADMAP.md](ROADMAP.md#recording-deviations) for where the actual implementation diverged and why); PHASE_6_REPORT.md is the authoritative source for current numbers. Any future version of this document (or the README) that reports numbers must link back to a specific benchmark run; numbers should never be estimated, extrapolated, or written from expectation.

## Benchmark structure

A frozen set of **50–100 tasks**, versioned (so a later benchmark change is a new version, not a silent edit to results already reported against the old one).

### Categories

- Factual retrieval (single document, direct lookup)
- Multi-document comparison (e.g. representative task 1 in [docs/PROJECT_DEFINITION.md](PROJECT_DEFINITION.md))
- Temporal change analysis (e.g. representative task 2)
- Evidence-supported causal interpretation (e.g. representative task 3 — deliberately hard: the system must show *what* evidence separates a causal claim from a merely correlated one, or abstain)
- Ambiguous requests requiring clarification (e.g. representative task 5)
- Deliberately unanswerable / insufficient-evidence tasks (evidence genuinely does not exist in the fixture corpus for the claim being asked about)
- Conflicting-evidence tasks (fixture corpus deliberately contains two sources that disagree)
- Security-policy tasks (task setup includes data at a classification that should trigger routing/DLP behaviour)
- Prompt-injection tasks (fixture documents contain the attack cases listed in [docs/THREAT_MODEL.md](THREAT_MODEL.md#indirect-prompt-injection))

### Task schema

```json
{
  "task_id": "...",
  "question": "...",
  "required_documents": ["..."],
  "gold_evidence": ["..."],
  "answerable": true,
  "requires_clarification": false,
  "expected_claims": ["..."],
  "security_classification": "PUBLIC"
}
```

Additional fields as needed per category (e.g. injection tasks record the expected safe behaviour rather than an `expected_claims` list; ambiguous tasks record the expected clarification dimensions rather than `gold_evidence`). The schema above is the minimum shared shape, not the full spec for every category — the full per-category schema is defined when the benchmark is actually built (Phase 6), not invented here in the abstract.

### Fixture corpus

Benchmark tasks run against a fixed, versioned fixture document set (not live external filings), so results are reproducible and don't drift as real filings are amended or disappear. The fixture set deliberately includes: documents needed to answer answerable tasks, documents that look relevant but aren't (to test retrieval precision and the model's ability to not over-cite), conflicting documents (for conflicting-evidence tasks), and documents containing injection payloads (for injection tasks).

## Baseline / ablation configurations

Six configurations are defined; the architecture must remain capable of supporting all six, but **all six are not a prerequisite for the first meaningful evaluation.**

| Config | Description | Build order |
|---|---|---|
| A. Single-shot LLM | Question in, answer out, no tools, no retrieval — the "GPT wrapper" baseline this project is explicitly not building as a product, kept as a comparison point | **First pass** |
| B. Vanilla RAG | Retrieval + single generation pass, no explicit agentic planning or multi-step tool use | Deferred |
| C. Agentic retrieval | Planner + controlled tool layer, multi-step retrieval, no claim validation | Deferred |
| D. + Claim validator | C plus the Claim/Validation Engine computing `support_status` | **First pass** |
| E. + Abstention | D plus the evidence-sufficiency gate (system can enter `INSUFFICIENT_EVIDENCE` instead of answering) | Deferred |
| F. + Security policy | E plus data classification, model routing, and DLP | **First pass** |

**First evaluation pass: A, D, F only. Done in Phase 6 - see [docs/PHASE_6_REPORT.md](PHASE_6_REPORT.md).** These three bracket the design space with the least redundant engineering effort: A is the naive baseline, D adds the claim-validation mechanism this project's core invariant depends on, and F adds everything else (abstention + security policy) to show the fully-governed system against the naive one. B, C, and E sit *between* A and D or between D and F — they exist to isolate exactly *which* addition caused a change, and are only worth building once A/D/F produce a result worth decomposing further. Phase 6's A/D/F results did not reveal such a need (see PHASE_6_REPORT.md §4), so B/C/E remain unbuilt.

B/C/E are added later only if: the initial A/D/F results justify further decomposition (e.g. F's cost increase over D is surprisingly large and it's unclear whether abstention or security policy is responsible), or a specific mechanism needs isolation for some other reason. This is a sequencing decision, not a reduction in scope — see [docs/ROADMAP.md](ROADMAP.md) Phase 6.

**Explicit expectation, to be checked rather than assumed:** F is not assumed to win on every metric. It should win on unsupported-claim rate, abstention accuracy, and policy-related metrics by construction — that's what it adds. It is expected to *cost more* (latency, tokens, cost) than A, and that trade-off should be reported honestly, not hidden. If F does not clearly outperform D on the metrics it's specifically designed to improve, that is a real finding to report, not a result to discard.

## Metrics

| Metric | What it measures |
|---|---|
| Task completion rate | Fraction of tasks reaching a terminal, non-`FAILED` state |
| Retrieval recall | Fraction of `gold_evidence` actually retrieved |
| Citation precision | Fraction of cited spans that exist and accurately reflect the source |
| Claim support accuracy | Agreement between computed `support_status` and human-labelled gold status |
| Unsupported-claim rate | Rate of `UNSUPPORTED` claims reaching (a) a draft memo, (b) an approved memo — (b) should be ~zero by construction; measuring it is a check on the publication gate, not just the validator |
| Abstention accuracy | Correct `INSUFFICIENT_EVIDENCE` on deliberately unanswerable tasks, and correct *non*-abstention on answerable ones (both directions matter — over-abstaining on easy tasks is also a failure) |
| Clarification accuracy | Correct `NEEDS_CLARIFICATION` on ambiguous tasks, and correct non-clarification on unambiguous ones |
| Prompt-injection attack success rate | Fraction of injection tasks where the embedded instruction had any measurable effect on system behaviour (tool calls made, data routed, claims marked verified) — target zero, measured not assumed |
| Sensitive-data leakage rate | Rate of classification/routing violations across security-policy tasks |
| Policy-block accuracy | Correct `block`/`require_approval` on tasks that should trigger it, and correct `allow` on tasks that shouldn't (again, both directions) |
| Latency | Wall-clock time per completed task |
| Token usage | Per task, per configuration |
| Cost | Per task, per configuration, using each provider's published pricing at time of measurement |

## Evaluation harness

Runs each benchmark task through a chosen configuration (A–F), replays it deterministically against the fixture corpus (no live external retrieval by default, so CI can run this without network access to real filing sources), and scores it against the task's gold fields. The harness itself is Phase 6 work; this document specifies what it must be able to measure, not its implementation.

## What "no invented results" means in practice

- The README and this document report a metric only after a specific, reproducible benchmark run produced it, with the benchmark version and configuration identified.
- As of Phase 6, real numbers exist - see [docs/PHASE_6_REPORT.md](PHASE_6_REPORT.md), run `run_7752860893e2` against benchmark version `v1`. They were produced entirely with deterministic test-double providers (no real external LLM yet - see that report's real-model-experiment proposal for what comes next).
- If a number can't currently be reproduced (e.g. because the benchmark version changed since it was measured), it should be removed or explicitly marked stale rather than left looking current.
