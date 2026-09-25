# Phase 7 Report

Status: **Complete.** 224/224 backend tests pass (`cd backend && python3 -m pytest -v`) - the 198 tests from Phase 1-6.5 plus 26 new (`tests/test_api.py`'s 25, plus one `Repository.list_tasks()` test in `tests/test_trace.py`). 27/27 frontend tests pass (`cd frontend && npm test`). The frontend production build succeeds (`npm run build`). No real external LLM was called anywhere in this phase; Phase 8 has not been started.

## The Phase 7 goal

Build the Research Assurance Console frontend - make the existing backend's assurance workflow (plan, evidence, claims, validation, security/policy decisions, human review, publication) visible, demonstrable, and portfolio-ready, without redesigning any backend semantics, without a chat-first interface, and without touching Phase 6's benchmark logic.

## 1. What was built

| Component | Location |
|---|---|
| Read/review HTTP API (FastAPI) | `backend/src/afra/api/app.py` |
| `Repository.list_tasks()` | `backend/src/afra/storage/repository.py` |
| API server entry point | `backend/scripts/run_api.py` |
| Demo data seed script (8 tasks, one per representative state) | `backend/scripts/seed_demo_data.py` |
| Backend API tests (25) | `backend/tests/test_api.py` |
| `list_tasks()` test | `backend/tests/test_trace.py` |
| Frontend (React + TypeScript + Vite) | `frontend/` |
| Frontend component/page tests (27, Vitest + React Testing Library) | `frontend/src/**/*.test.tsx` |

## 2. The API layer

Exactly as thin as `docs/ARCHITECTURE.md`'s Phase 0 design already specified for it ("Contains no business logic - every rule below lives in the orchestrator, validation engine, or policy layer, not in a route handler"):

- `GET /api/tasks` - every persisted task, most recent first, with `publication_allowed` computed by calling the real, unmodified `afra.review.publication_gate.evaluate_publication_gate()` per task.
- `GET /api/tasks/{task_id}` - `afra.trace.assemble_trace()`'s existing bundle (plan, tool calls, routing decisions, evidence, claims + validation, security events, reviews, state transitions, model calls) plus `publication_allowed`, `publication_gate_reasons` (both from the same publication-gate call), and `stale_approval` (a direct comparison of the latest APPROVE review's `claim_set_version` against the task's current one - the same condition the publication gate already checks, surfaced so the frontend doesn't recompute the rule itself).
- `POST /api/tasks/{task_id}/review`, `/publish`, `/resume` - call `ResearchTaskOrchestrator.submit_review()`/`publish()`/`resume_from_revision()` directly. No new validation, no new state logic.
- `GET /api/evaluation` - reads the most recently modified `backend/benchmark/results/<run_id>/` directory from disk (never a hardcoded number) plus `backend/benchmark/real_model_subset_v1.json`, and reports `real_model_validation.status` as unconditionally `"pending"`.

Every `AfraError` subclass (`TaskNotFoundError`, `IllegalTransitionError`, `SelfApprovalError`, `UnauthorizedReviewerError`, `SecurityPolicyError`, `PublicationGateError`) is caught by two global exception handlers and mapped to `404`/`400` with the existing exception's own message - no new error taxonomy.

**One genuinely new backend read primitive**: `Repository.list_tasks()`. Everything else the API needed already existed (`get_task`, `assemble_trace`, `evaluate_publication_gate`, the three orchestrator write methods).

Providers are exclusively `afra.providers.test_double`'s deterministic doubles (`TestDoubleProvider`/`EnterpriseApprovedTestDoubleProvider`/`PrivateLocalTestDoubleProvider`) - `afra.providers.real_model` is not imported anywhere in `afra.api` or the seed script.

## 3. Demo data

No task-creation UI or endpoint exists yet (a deliberate scope narrowing from `docs/ARCHITECTURE.md`'s original API-layer description - see `docs/ROADMAP.md`'s Phase 7 deviation entry). Instead, `backend/scripts/seed_demo_data.py` calls `ResearchTaskOrchestrator` directly - the same way Phase 1-6's own tests and the Phase 6 benchmark harness do - to produce eight tasks, one per representative state:

| State | How it's produced |
|---|---|
| `PUBLISHED` | `run_autonomous_research()` → `submit_review(APPROVE)` → `publish()` |
| `AWAITING_REVIEW` | `run_autonomous_research()`, left unreviewed |
| `NEEDS_CLARIFICATION` | An ambiguous-axis comparison question |
| `REJECTED` | `run_autonomous_research()` → `submit_review(REJECT)` |
| `REVISION_REQUESTED` | `run_autonomous_research()` → `submit_review(REQUEST_REVISION)` |
| `APPROVED` (stale approval) | Real approval at `claim_set_version=1`, then the version is bumped directly and re-saved - the exact hand-construction technique `tests/test_phase5_review_workflow.py`'s own stale-approval test already uses, since no live orchestrator path produces this from `APPROVED` |
| `INSUFFICIENT_EVIDENCE` | The Phase 6 benchmark's `CE-01` manual-conflicting-claim construction (CONTOSO-2024-10K vs CONTOSO-2025-10K capex outlook) |
| `SECURITY_BLOCKED` | The Phase 6 benchmark's `SD-03` manual-document-override construction (`CONTOSO-MNPI-NOTE`, RESTRICTED) |

Verified live in a browser during this phase: every panel (overview, state timeline, plan/tool activity, evidence, claims/validation, security/policy, human review, audit trace) renders correctly against all eight states, and a real approve → publish round-trip through the UI updates the backend and re-renders the new state without a page reload.

## 4. Frontend

React + TypeScript + Vite, `react-router-dom` for the three routes (`/tasks`, `/tasks/:taskId`, `/evaluation`), plain CSS (no UI framework - a dense, bordered-table, badge-driven internal-console look, not a consumer chat aesthetic). `frontend/src/api/types.ts` mirrors the backend's JSON shapes exactly; `frontend/src/api/client.ts` is a thin `fetch` wrapper, one function per endpoint.

Every requirement from the Phase 7 spec's "Core screens/components" list maps to a real, backend-sourced field - nothing rendered is computed or invented client-side: overview (task/scope/classification/version/publishable-now), state timeline (rendered only from `state_transitions`, never a fixed happy-path list - see `StateTimeline.test.tsx`), plan/tool activity, evidence (with "used by a claim" computed from the real evidence-link set), claims & validation (citation status, support status, quoted spans), security/policy (with the required distinction between prompt-injection detection as an audit signal and structural policy enforcement as the actual defence, stated in-panel), human review (decision history, stale-approval flag, the three review actions), and audit trace (a flattened, chronological view over the same persisted rows).

## 5. Evaluation page

Reads `GET /api/evaluation` and renders exactly what's on disk: the Phase 6 A/D/F table (labelled "Deterministic benchmark - synthetic fixture corpus... no real external LLM was used"), and a Phase 6.5 panel labelled **PENDING** with the frozen 14-task/7-category subset's actual contents. Verified live: after this session's Phase 6 documentation reconciliation, the page correctly shows D's `unsupported_claim_rate` as `16%`, read live from `run_f3bb065c08c9` - not a hardcoded number that could go stale the next time the benchmark is re-run.

## 6. Testing

- `backend/tests/test_api.py` (25 tests): every endpoint, plus the specifically required cases - a `SECURITY_BLOCKED`/`INSUFFICIENT_EVIDENCE` task is never `publication_allowed`; a stale approval displays as stale and a fresh one doesn't; `REVISION_REQUESTED` shows both resume paths and version history across a revision cycle; `/api/evaluation` always reports `real_model_validation.status == "pending"` and never renders the word "complete".
- `frontend/src/pages/*.test.tsx`, `frontend/src/components/*.test.tsx` (27 tests): the same required cases re-verified at the rendering layer (e.g. `TaskDetail.test.tsx`'s "TaskDetail - publication safety" and "TaskDetail - stale approvals" describe blocks), plus `StateTimeline.test.tsx::"renders only the states actually reached, not a fake happy path"`.
- Both suites run against mocked/fixture data shaped exactly like the real API's verified JSON output (checked live via `TestClient` and a running server during development), not invented shapes.

## 7. How to run it

```bash
# Backend API
cd backend
python3 -m pip install -e ".[api]"
python3 scripts/seed_demo_data.py        # populates backend/afra_demo.db
python3 scripts/run_api.py               # http://127.0.0.1:8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev                              # http://localhost:5173 (proxies /api to the backend above)

# Tests
cd backend && python3 -m pytest -v       # 224 tests
cd frontend && npm test                  # 27 tests
cd frontend && npm run build             # production build
```

## 8. Overengineering flags

1. **The three-test-double-provider-registry construction is now duplicated a third time** (`afra.benchmark.configurations.build_full_provider_registry()`, `tests/conftest.py`'s `providers` fixture, and now `afra/api/app.py` / `scripts/seed_demo_data.py`'s own copies) - five lines each, not factored into a shared helper in this phase. Worth revisiting if a fourth call site appears; not done here to avoid adding a new shared module for something this small.
2. **No task-creation UI or endpoint** - disclosed in `docs/ROADMAP.md`'s Phase 7 deviation entry, not silently assumed to be out of scope.

None of the above blocks Phase 8.
