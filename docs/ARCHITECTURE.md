# Architecture

Nothing in this document is implemented yet (Phase 0). This is the design the later phases build against, and the thing later phases are allowed to push back on — see the end of [docs/ROADMAP.md](ROADMAP.md) for how deviations get recorded.

## Design principle

```text
LLMs interpret.
Tools retrieve.
Deterministic systems verify.
Humans approve.
Policies control what data each model is allowed to see.
```

Every component below exists to make one clause of that sentence true in code, not just in a prompt.

## Component map

```text
                         ┌─────────────────────────────┐
                         │  Research Assurance Console  │   (Phase 7)
                         │  React + TypeScript + Vite   │
                         └───────────────┬──────────────┘
                                         │ REST/HTTP
                         ┌───────────────▼──────────────┐
                         │        FastAPI API layer      │
                         └───────────────┬──────────────┘
                                         │
                         ┌───────────────▼──────────────┐
                         │   Task Orchestrator (state    │   (Phase 1)
                         │   machine engine)              │
                         └───┬───────┬───────┬───────┬───┘
                             │       │       │       │
                 ┌───────────▼┐ ┌────▼────┐ ┌▼────────┐ ┌▼───────────────┐
                 │  Research  │ │Controlled│ │Claim /   │ │ Data Classification│
                 │  Planner   │ │Tool Layer│ │Validation│ │ & Model Routing     │
                 │ (Phase 2)  │ │(Phase 2) │ │Engine    │ │ Policy (Phase 4)    │
                 └───────────┬┘ └────┬────┘ │(Phase 3) │ └──────────┬─────────┘
                             │       │       └────┬─────┘            │
                             │       │            │                  │
                             │  ┌────▼────┐        │        ┌─────────▼─────────┐
                             │  │ Evidence │◄───────┘        │  DLP / Security    │
                             │  │  Store   │                 │  Policy (Phase 4)  │
                             │  └────┬────┘                  └─────────┬─────────┘
                             │       │                                 │
                             │       │                       ┌─────────▼─────────┐
                             │       │                       │ Model Provider     │
                             └───────┴──────────────────────►│ Abstraction        │
                                                              └─────────┬─────────┘
                                                                        │
                                                    ┌───────────────────┼───────────────────┐
                                                    │                   │                   │
                                           Deterministic      Gemini / Antigravity   OpenAI-compatible /
                                           test doubles       CLI claim drafting     self-hosted adapters
                                           (implemented)      (implemented)          (future work)

                         ┌────────────────────────────────────────────────────────┐
                         │         Review / Approval Service (Phase 5)             │
                         └────────────────────────────────────────────────────────┘
                         ┌────────────────────────────────────────────────────────┐
                         │         Audit Trace Store (cross-cutting, Phase 1+)      │
                         └────────────────────────────────────────────────────────┘
```

## Components

### API layer (FastAPI)

Thin HTTP boundary: create/resume research tasks, submit clarification answers, read plan/evidence/claim/trace state, submit review decisions. Contains no business logic — every rule below lives in the orchestrator, validation engine, or policy layer, not in a route handler, so it can be unit-tested without HTTP.

### Task Orchestrator

Owns the state machine defined in [docs/STATE_MACHINE.md](STATE_MACHINE.md). Responsible for: persisting task state after every transition, resuming a task from persisted state (not from conversation memory) after clarification or review, and enforcing which transitions are legal. The orchestrator is the only component allowed to change a task's `final_status`.

### Research Planner

Turns a (possibly clarified) research question into a plan: which companies/documents/time periods are in scope, and which tools need to run in what order. Detects ambiguity (see representative task 5 in [docs/PROJECT_DEFINITION.md](PROJECT_DEFINITION.md#3-representative-tasks)) and raises a clarification request instead of guessing. The planner proposes tool calls; it does not execute them and does not see raw evidence content — it operates on document/section metadata.

### Controlled Tool Layer

The only way the system touches external data. Exposes a fixed set of named tools (see below) with typed inputs/outputs. The model can request a tool call; the tool layer executes it, applies data-classification tagging to whatever it returns, and passes results back as **data**, never as instructions (see [docs/THREAT_MODEL.md](THREAT_MODEL.md#indirect-prompt-injection)). No tool in this layer can publish, send external communications, or write to the approved knowledge base.

Planned tools:

| Tool | Purpose |
|---|---|
| `search_filings()` | Find candidate filings by company/date/type |
| `get_document()` | Fetch a specific filing by identifier |
| `retrieve_section()` | Fetch a named section (e.g. risk factors) from a document |
| `retrieve_evidence()` | Semantic/lexical retrieval of passages relevant to a sub-question |
| `compare_sections()` | Structured diff between two sections across filings/periods |
| `build_evidence_table()` | Assemble retrieved passages into a structured evidence table |
| `validate_citations()` | Check that a cited passage exists and says what it's cited as saying |
| `check_claim_support()` | Compute a claim's support status against linked evidence |
| `generate_research_memo()` | Draft memo text from claims + evidence (drafting only — not publication) |

Later, optional: `search_sec_filings()`, `get_company_metadata()`, `get_financial_metric()` — not required to prove the core idea and deferred past Phase 2 unless a benchmark task needs them.

### Evidence Store

Persists retrieved passages as first-class `Evidence` records (see [docs/CLAIM_EVIDENCE_MODEL.md](CLAIM_EVIDENCE_MODEL.md)), each carrying source document identity, location, timestamp, and data classification. This is the layer other Fengzhe Li projects (e.g. FKIP-style retrieval) could plug into — see [Relationship to FKIP](#relationship-to-fkip).

### Claim / Validation Engine

Deterministic (non-LLM-self-reported) step that takes a drafted claim plus its linked evidence and computes a `support_status` (`SUPPORTED` / `PARTIALLY_SUPPORTED` / `UNSUPPORTED` / `CONFLICTING_EVIDENCE` / `INSUFFICIENT_EVIDENCE`). An LLM call may be *part of* how a validator checks entailment between claim text and evidence text, but the validator's *output* is a structured, stored result the application logic branches on — the model never gets to write directly into `approval_status`.

### Data Classification & Model Routing Policy

Before any model call, this layer determines the classification of the data involved and looks up which model providers/endpoints are allowed to see data of that classification. This is a policy lookup, not a hardcoded `if classification == "RESTRICTED"` scattered through the codebase — see [docs/DATA_CLASSIFICATION.md](DATA_CLASSIFICATION.md#policy-abstraction) for the policy object shape.

### DLP / Security Policy

Scans content (retrieved evidence, drafted claims, tool outputs) for sensitive-content categories (PII, client identifiers, account numbers, unpublished figures, internal project names, confidential notes, restricted attachments) and applies one of `allow` / `redact` / `block` / `route_private` / `require_approval`. Runs both on data *entering* a model call and on content that would exit the system (e.g. into a memo). See [docs/DATA_CLASSIFICATION.md](DATA_CLASSIFICATION.md).

### Model Provider Abstraction

A single interface — `ModelProvider.complete(purpose, prompt)` in `backend/src/afra/providers/base.py` — that every other component (planner, validator, memo drafting) depends on instead of a specific vendor SDK. Implemented adapters:

- **Deterministic test doubles** (`afra.providers.test_double`) — used for every purpose in CI and in the Phase 6 benchmark.
- **Real claim-drafting adapters** (`afra.providers.real_model`) — `GeminiClaimDraftProvider` (Gemini API via `google-genai`) and `AntigravityClaimDraftProvider` (headless Antigravity CLI, used for the canonical Phase 6.5 run). Both handle `purpose="draft_claim"` only; `HybridDraftClaimProvider` routes that purpose to a real adapter and every planning/interpretation purpose to a deterministic test double.

An earlier Anthropic adapter was retired before any live run (see [PHASE_6_5_REAL_MODEL_REPORT.md §0.5](PHASE_6_5_REAL_MODEL_REPORT.md)). OpenAI-compatible and self-hosted adapters are **not implemented**; they are the intended next adapters behind the same interface.

**Design test:** if every real adapter is deleted, the orchestrator, state machine, claim model, validation engine, classification policy, and audit trace are all still meaningful and still testable (the test-double adapters are enough to exercise them). If that stops being true, the architecture has quietly become vendor-coupled and needs fixing.

### Review / Approval Service

Where a senior reviewer sees a draft memo's claims, evidence, and validation results, and records a decision (`AWAITING_REVIEW` → `APPROVED` or `REVISION_REQUESTED`). Enforces reviewer-separation-from-author where required (see [docs/CLAIM_EVIDENCE_MODEL.md](CLAIM_EVIDENCE_MODEL.md#reviewer-separation)). This service, not the model, is what sets `approval_status`.

### Audit Trace Store

Every research run persists a structured trace (plan, tool calls, retrieved evidence, draft claims, validation results, security events, final status, model id, latency, tokens, cost — see [`ModelCall`](CLAIM_EVIDENCE_MODEL.md#modelcall) and [`SecurityEvent`](CLAIM_EVIDENCE_MODEL.md#securityevent) in docs/CLAIM_EVIDENCE_MODEL.md). Cross-cutting: every other component writes to it; nothing reads it to make decisions (it is a record, not a control input), which keeps it safe to expose to the compliance/AI-governance reviewer without it being able to influence future runs.

## Relationship to FKIP

`financial-knowledge-intelligence-platform` owns document ingestion, validation, indexing, and retrieval infrastructure. This project's `retrieve_section()` / `retrieve_evidence()` tools are the boundary where this project could call into FKIP-style retrieval rather than reimplementing it — but this project defines its own `Evidence` record shape (classification, claim linkage, audit fields) independent of how the underlying retrieval is implemented, so FKIP can be swapped for a different retrieval backend, a fixture set, or a mock without changing anything above the tool layer.

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Backend | Python, FastAPI | |
| Data | PostgreSQL | System of record for tasks, claims, evidence, reviews, audit trace |
| Vector/semantic retrieval | pgvector or Qdrant, if/when retrieval quality actually requires it | Not added by default — see below |
| Frontend | React, TypeScript, Vite | Research Assurance Console (Phase 7), not a chat UI |
| Infra | Docker / Docker Compose | Must remain runnable locally |
| CI | GitHub Actions | |
| Testing | pytest; frontend tests as appropriate | Includes failure-injection and prompt-injection test suites, not just happy-path tests |

### On Redis / queues / background workers

Not included by default. A research task is expected to run over multiple tool calls and possibly a human clarification/review round-trip, which is naturally asynchronous — but that can be modeled as **persisted task state plus polling/resume**, which the state machine already requires, without a queue. A queue or background worker would only be justified if: (a) task volume requires work to be distributed across multiple processes, or (b) a tool call needs to run longer than an HTTP request timeout allows without blocking the API process. Neither is established yet. If Phase 2+ implementation shows one of these is real, add it then and record the justification in [docs/ROADMAP.md](ROADMAP.md) rather than assuming it up front.

### On pgvector/Qdrant

Same logic: only add a vector store when `retrieve_evidence()` actually needs semantic similarity search over a corpus large enough that lexical/section-based retrieval isn't sufficient for the benchmark tasks. Phase 2's initial implementation should start with the simplest retrieval that can pass benchmark tasks 1–4 in [docs/PROJECT_DEFINITION.md](PROJECT_DEFINITION.md#3-representative-tasks), and only add a vector store if that proves insufficient.
