# Phase 5 Report

Status: **Complete.** 120/120 tests pass (`cd backend && python3 -m pytest -v`), up from 104 at the end of Phase 4 (13 new tests in `test_phase5_review_workflow.py`, 1 new direct gate test in `test_publication_gate.py`, 2 new state-machine tests for the new `REJECTED` terminal state, 1 existing state-machine test fixed to derive its terminal-state list from `TERMINAL_STATES` instead of a hand-maintained tuple).

## The Phase 5 success criterion

> AI-generated research can only become final institutional output after explicit authorized review of the exact validated version being published.

Phase 1 already proved unsupported claims can't reach `PUBLISHED`; Phase 4 already proved unsafe content can't reach a model. Phase 5 closes the remaining gap in that sentence: *explicit authorized review of the exact validated version*. Before this phase, a reviewer had only `approve`/`request_revision`, self-approval was the only checked conflict of interest, reviewer identity was unchecked, and nothing tied an approval to a specific version of the claim set it actually looked at.

## What was built

- **`ReviewDecision.REJECT`** and a new terminal `TaskState.REJECTED`, reachable from `AWAITING_REVIEW` — a hard rejection on the merits, distinct from `REQUEST_REVISION`'s resumable cycle.
- **Separation of duties broadened to every decision**: `reviewer_id != created_by` is now checked for `approve`, `reject`, and `request_revision` alike (Phase 1-4 only checked it for `approve`).
- **`afra.identity`** — a new, small module: `AUTHORIZED_REVIEWER_IDS` (`reviewer_1`, `governance_admin`) and `GOVERNANCE_REVIEWER_IDS` (`governance_admin`), checked both in `submit_review()` (fails fast) and independently in the publication gate (defence in depth).
- **`ResearchTaskOrchestrator.resume_from_revision(task_id, needs_new_evidence=False)`** — the real, public entry point back from `REVISION_REQUESTED` into `SYNTHESISING` or `GATHERING_EVIDENCE`, on the same `task_id`, replacing what would otherwise have needed direct access to the private `_transition()`.
- **Version integrity**: `ResearchTask.claim_set_version` (int, incremented every `complete_synthesis()` call) and `Review.claim_set_version` (stamped at review time). The publication gate now fails if the approval on record was made against an earlier version than the task's current one.
- **`Review.security_context`** — a short, deterministic summary of the task's `SecurityEvent` history, stamped at review time for the audit record.
- **`ResearchTaskOrchestrator.resolve_security_event(security_event_id, resolved_by)`** — the real resolution workflow for a `require_approval` `SecurityEvent`, restricted to `GOVERNANCE_REVIEWER_IDS`, raising if applied to a `block` event (no override exists for an outright block) — and deliberately not touching task state.
- **Publication gate**: two new independent clauses — reviewer authorization, and claim-set-version match — alongside the existing state/claims/security/self-approval checks.
- **`security_events` / `reviews` schema**: `resolved_by` updates via a dedicated `resolve_security_event()` repository method; `reviews` gained `claim_set_version` and `security_context` columns; `research_tasks` gained `claim_set_version`.

## One approve → publish path

```text
CREATED -> PLANNING -> GATHERING_EVIDENCE -> SYNTHESISING -> VALIDATING -> AWAITING_REVIEW -> APPROVED -> PUBLISHED
```

```json
[
  { "decision": "approve", "reviewer_id": "reviewer_1", "claim_set_version": 1 }
]
```

(`backend/scripts/print_phase5_traces.py`, scenario 1.)

## One request-revision → revalidate → reapprove path

```text
CREATED -> PLANNING -> GATHERING_EVIDENCE -> SYNTHESISING -> VALIDATING -> AWAITING_REVIEW -> REVISION_REQUESTED -> SYNTHESISING -> VALIDATING -> AWAITING_REVIEW -> APPROVED -> PUBLISHED
```

```json
[
  { "decision": "request_revision", "claim_set_version": 1, "comments": "Add a second corroborating citation." },
  { "decision": "approve", "claim_set_version": 2, "comments": "" }
]
```

The same `task_id` runs the entire path — `resume_from_revision()` is what takes the task from `REVISION_REQUESTED` back into `SYNTHESISING`, where a second `draft_claim()` call and a second `complete_synthesis()` bump `claim_set_version` from 1 to 2. The two persisted `Review` rows show exactly which version each decision evaluated. (Scenario 2.)

## One stale-approval rejection

Hand-constructed, the same way Phase 1's and Phase 4's own not-yet-reachable gate clauses are proven — see "What's real vs. what's fixture" below for why this one has no live orchestrator path either. A task is set to `APPROVED` at `claim_set_version=2`, with its one `Review` recorded at `claim_set_version=1`:

```text
gate.passed=False
reasons=["approval was recorded for claim_set_version=1, but the task's current
  claim_set_version is 2 - the claim set changed since this approval, so it no
  longer authorizes the current version (stale approval)"]
```

(Scenario 3. `test_stale_approval_cannot_authorize_a_modified_claim_set` is the corresponding test.)

## One security-blocked case that approval cannot bypass

`CONTOSO-MNPI-NOTE` (`RESTRICTED`) evidence:

```text
SecurityPolicyError raised during drafting: RESTRICTED classification has no
  default model routing (docs/DATA_CLASSIFICATION.md#model-routing-policy);
  no policy exception is recorded for this task.
task.state=SECURITY_BLOCKED
submit_review() raised IllegalTransitionError: cannot review while task is in SECURITY_BLOCKED
task.state after attempted review=SECURITY_BLOCKED (unchanged)
```

No special-cased "is this task security blocked" check exists in `submit_review()` — the state machine itself is the enforcement: `submit_review()` requires `state == AWAITING_REVIEW`, and `SECURITY_BLOCKED` has zero outgoing transitions, so no reviewer (whatever their identity or authorization) can move a security-blocked task anywhere. (Scenario 4. `test_security_blocked_task_cannot_be_reviewed_or_approved` and `test_resolving_a_require_approval_security_event_does_not_revive_a_blocked_task` are the corresponding tests — the second proves that even *resolving* the underlying `SecurityEvent` doesn't change this.)

## Real enforcement vs. fixture identity

**Real:**
- Every gate clause (state, claims, security, self-review, reviewer authorization, version integrity) is independently checked application logic against persisted state, re-evaluated at the moment of publication — not a cached flag, not a prompt instruction.
- `resume_from_revision()` is a real, public state transition on the same persisted task; nothing about the revision loop restarts the task or creates a new one.
- `claim_set_version` is a real, persisted, monotonically incremented counter, and the gate's version-match check is real comparison logic against it.
- `resolve_security_event()` really persists `resolved_by` and really requires an authorized identity to call — it is not a no-op — but see below for what it does *not* do.
- Self-review rejection (`reviewer_id == created_by`) is real, enforced at the data/domain layer, exercised directly by tests, unchanged in kind from Phase 1 — only broadened in scope (every decision, not just `approve`).

**Fixture identity, not authentication:**
- `AUTHORIZED_REVIEWER_IDS` and `GOVERNANCE_REVIEWER_IDS` are hardcoded Python sets of two and one string respectively. Nothing verifies that a caller passing `reviewer_id="governance_admin"` actually is that person — there is no login, session, token, or credential anywhere in this project. This is exactly as strong (and exactly as limited) as `reviewer_id != created_by`: a real, enforced constraint on the *values supplied*, with zero claim about who supplied them.
- This is a narrowing of one sentence in Phase 1's original identity-scope documentation ("who is allowed to act as `governance_admin` ... is a deployment/configuration concern this project does not address"), made because the Phase 5 spec explicitly asked for a "reviewer is authorized" gate condition distinct from separation of duties. It is recorded as a deviation in `docs/ROADMAP.md`, not a silent contradiction — see that entry for the full reasoning.
- `resolve_security_event()`'s effect is entirely audit-record-keeping. It sets a column. It does not, and structurally cannot, move a task out of `SECURITY_BLOCKED` — that state has no outgoing transitions in `afra.orchestrator.state_machine.ALLOWED_TRANSITIONS`, full stop, independent of anything `resolve_security_event()` does. Calling this "resolution" should not be read as "the block is lifted" — only as "a governance identity has been recorded as having looked at it."

## Overengineering flags

1. **The stale-approval and reviewer-authorization gate clauses have no live orchestrator path**, exactly like Phase 4's SecurityEvent clause before them. `APPROVED` only transitions to `PUBLISHED` in this state machine — there is no route back from `APPROVED` to a modified claim set through any public orchestrator method, so a real "approve, then someone else modifies the claims, then a third party tries to publish" sequence cannot currently happen through the API surface this project exposes. Both clauses are implemented and directly unit-tested against hand-constructed repository state (the same honest pattern already used repeatedly in this codebase), not because a live path has been observed. This is disclosed rather than presented as more load-bearing than it currently is.
2. **`claim_set_version` is a bare counter, not a content fingerprint.** It answers "has this claim set been finalised again since this approval" correctly, but would not catch a claim mutated in place without going through `complete_synthesis()` again. No such in-place mutation path exists today (`record_validation_result()` is the only thing that touches a `Claim` after creation, and it never touches `claim_text`), so the simpler design is sufficient for what's actually testable — but it is a real, named tradeoff, not an oversight; see the recorded deviation in `docs/ROADMAP.md`.
3. **`resolve_security_event()` is real code with a genuinely narrow live effect** (see "Real enforcement" above). Building it was required by the Phase 5 spec's explicit "add the real workflow that resolves that block" instruction, while the spec's own item 7 forbade inventing an override path. The result is a feature that does something true and auditable (governance sign-off is now a real, checkable fact) but does not do the more dramatic thing its name might suggest (revive a blocked task) - worth a second read of `docs/CLAIM_EVIDENCE_MODEL.md`'s `resolved_by` field note before assuming otherwise.
4. **No "supersede a prior claim" concept was added for the revision loop.** A `REQUEST_REVISION` cycle can only *add* claims via a fresh `draft_claim`/`draft_claim_from_evidence` call in the new `SYNTHESISING` pass; there is no way to retire or replace a specific earlier claim the reviewer objected to. Combined with the still-unchanged Phase 1 simplification that "every `Claim` belonging to the task is required" for the publication gate, a revision cycle that needs to *remove* a claim (rather than add a corrected one alongside it) isn't supported yet. This is the same, previously-disclosed gap (see the Phase 1 entry in `docs/ROADMAP.md`), not a new one, but it's more visible now that a real revision loop exists to run into it.

None of the above blocks Phase 6.

## What remains deferred

Unchanged from Phase 4's list: the remaining 6 tools, generalising the validator beyond one quote-span check, a real external model provider, the frontend, Docker, the benchmark/ablation harness, enterprise authentication, new DLP detectors or security semantics (explicitly out of scope for this phase — Phase 4's DLP/routing behaviour is unchanged). See `docs/ROADMAP.md` Phase 6 onward.

## How to run it

```bash
cd backend
python3 -m pip install -e ".[dev]"
python3 -m pytest -v                          # 120 tests
python3 scripts/print_phase5_traces.py        # regenerate the four traces above
```

Still no network access, API key, Docker, or Postgres server required.
