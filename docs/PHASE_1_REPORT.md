# Phase 1 Report

Status: **Complete.** 30/30 tests pass (`cd backend && python3 -m pip install -e ".[dev]" && python3 -m pytest -v`).

This report states plainly what is real, what is fixture/test-double based, and what remains deferred, per the request that accompanied this phase's approval. It should be read alongside the Phase 1 row in [docs/ROADMAP.md](ROADMAP.md) for the scope this was checked against.

## The invariant this phase proves

> An AI-generated claim unsupported by evidence cannot reach `PUBLISHED`.

Proven by two independent test layers, not one:

- **Direct unit tests** of the publication gate (`backend/tests/test_publication_gate.py`), constructed by hand against the repository — the gate is checked as a pure function, with no orchestrator or state-machine flow involved.
- **End-to-end tests** through the full orchestrator (`backend/tests/test_end_to_end_slice.py`), including a case that forces a task into `AWAITING_REVIEW` → `APPROVED` with an `INSUFFICIENT_EVIDENCE` claim by writing directly to the repository (bypassing the sufficiency gate, which the orchestrator's own API would normally prevent) specifically to confirm that the *publication* gate — not just the sufficiency gate — is what actually protects `PUBLISHED`.

## What's real

- **The full state machine** from `docs/STATE_MACHINE.md` — all 13 states, the complete transition table, and the invariants (`FAILED` reachable from any non-terminal state; `PUBLISHED` reachable only via `APPROVED`; `APPROVED` reachable only via `AWAITING_REVIEW`; `SECURITY_BLOCKED`/`PUBLISHED`/`FAILED` terminal) are implemented in `afra/orchestrator/state_machine.py` and directly unit-tested in `tests/test_state_machine.py`, independent of the orchestrator.
- **Real, on-disk, process-restart-durable persistence.** SQLite, not an in-memory store — see the recorded deviation in `docs/ROADMAP.md` for why SQLite rather than the documented PostgreSQL target. `tests/test_resumability.py` proves this concretely: it closes the database connection and orchestrator entirely, constructs a brand-new `Repository`/`Orchestrator` pair with no Python object in common with the first, and shows `NEEDS_CLARIFICATION` resumes correctly from disk alone.
- **One real tool**, `get_document()`, against a small fixture corpus (two fictional companies, `afra/tools/fixtures.py`) — real code, real (if trivial) retrieval logic, real classification tagging, real content-hashing.
- **One real, deterministic validator** (`afra/validation/validator.py`) that computes `support_status` from citation-checkable facts (does the cited quote span actually appear in the evidence text; do supporting/contradicting links agree). It takes only `claim_id` and a repository — structurally, it cannot see or depend on which model (if any) drafted the claim; `tests/test_validator.py::test_validation_result_independent_of_claim_drafting_model` asserts this via the function's signature.
- **`ClaimEvidenceLink` as the authoritative claim/evidence relation**, and **`ValidationResult` as the authoritative support record**, per the clarifications in `docs/CLAIM_EVIDENCE_MODEL.md`. `Repository.record_validation_result()` is the single code path that ever writes `Claim.support_status`/`support_strength` — always as a side effect of persisting a `ValidationResult`, never independently.
- **The publication gate**, evaluated against current persisted state (not cached from when review started), including real reviewer-separation enforcement (`reviewer_id != created_by`) against named local fixture identities (`analyst_1`, `reviewer_1`) — a real, tested rejection, not a UI convention.
- **The model provider abstraction** and a real (if deterministic) implementation of it, `TestDoubleProvider` — every `draft_claim()` call genuinely goes through `ModelProvider.complete()` and produces a real, persisted `ModelCall` audit row.

## What's fixture / test-double based (real code, not real-world data or a real model)

- **The fixture document corpus** (`CONTOSO-2025-10K`, `FABRIKAM-2025-10K`) is entirely fictional — two invented companies, invented filing text, not real financial filings. There are only 2 documents, each with a single `risk_factors` section.
- **The model provider is `TestDoubleProvider`**, not a real external LLM. It performs no network call and deterministically echoes its prompt back prefixed with a purpose label. No OpenAI/Anthropic/self-hosted adapter exists yet.
- **Claim drafting is externally driven, not autonomously planned.** There is no planner or ambiguity-detection algorithm yet (that's Phase 2). `request_clarification()` and the content of `draft_claim()`'s prompt are supplied directly by the caller (in Phase 1, that's a test) standing in for what a real planner/drafting step will eventually decide. The state machine and resumability are real; the intelligence that would normally decide *when* to ask for clarification or *what* claim to draft is not implemented yet.
- **Reviewer/analyst identities are config values, not authentication.** `analyst_1`/`reviewer_1` are just strings passed into function calls — see `docs/CLAIM_EVIDENCE_MODEL.md#identity-scope-portfolio-implementation`. Reviewer separation is genuinely enforced against whatever identity is supplied; nothing verifies that a caller *is* who they claim to be.

## What remains deferred (explicitly, not silently)

- The remaining 8 tools (`search_filings`, `retrieve_section`, `retrieve_evidence`, `compare_sections`, `build_evidence_table`, `validate_citations`, `check_claim_support`, `generate_research_memo`) — Phase 2.
- The Research Planner / ambiguity-detection algorithm — Phase 2.
- A real external model provider adapter — Phase 2.
- Generalising the validator beyond one deterministic quote-span check (multi-evidence aggregation at scale, more nuanced conflicting-evidence handling) — Phase 3.
- Data classification enforcement and DLP/security policy — the `classification` field exists on `Evidence` (populated as `PUBLIC` for every fixture document) but nothing reads or enforces it yet; `SECURITY_BLOCKED` is implemented in the state machine but structurally unreachable in Phase 1. Phase 4.
- The documented "disclosed partial support" exception to the publication gate — Phase 1's gate rejects `PARTIALLY_SUPPORTED` outright, conservatively, because the memo/section concept that exception depends on doesn't exist yet.
- `REVISION_REQUESTED` handling beyond the bare transition (no revision workflow, no comments surfaced anywhere yet) — Phase 5.
- Benchmark, ablation harness, frontend, Docker, CI — Phases 6–8.

## How to run it

```bash
cd backend
python3 -m pip install -e ".[dev]"
python3 -m pytest -v
```

No network access, no API key, no Docker, and no Postgres server are required. The full suite runs in well under a second.
