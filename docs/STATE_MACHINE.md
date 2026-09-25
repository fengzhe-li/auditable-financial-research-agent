# State Machine

The `ResearchTask` lifecycle is an explicit, persisted state machine. The orchestrator (see [docs/ARCHITECTURE.md](ARCHITECTURE.md#task-orchestrator)) is the only component that may write `final_status`/`state`. No other component — planner, tool layer, validator, or the model itself — may transition a task.

## States

| State | Meaning | Terminal? |
|---|---|---|
| `CREATED` | Task submitted, not yet processed | No |
| `PLANNING` | Ambiguity detection + research plan construction in progress | No |
| `NEEDS_CLARIFICATION` | Waiting on analyst to resolve detected ambiguity | No (resumable) |
| `GATHERING_EVIDENCE` | Controlled tool layer executing retrieval per the plan | No |
| `SYNTHESISING` | Draft claims being generated from retrieved evidence | No |
| `VALIDATING` | Claim/citation validation and evidence-sufficiency check running | No |
| `INSUFFICIENT_EVIDENCE` | Validation determined evidence does not support required claims at the needed strength | Conditionally (see below) |
| `SECURITY_BLOCKED` | DLP/security policy blocked the task | Yes |
| `AWAITING_REVIEW` | Draft memo assembled, sufficiency + security passed, waiting on human reviewer | No (resumable) |
| `REVISION_REQUESTED` | Reviewer sent the draft back with required changes | No (resumable) |
| `APPROVED` | Reviewer approved; publication gate conditions met | No |
| `REJECTED` | Reviewer rejected the research on the merits (distinct from a revision request) | Yes. **Phase 5 addition, not in the original table** — see [docs/ROADMAP.md](ROADMAP.md#recording-deviations) |
| `PUBLISHED` | Memo published to the approved knowledge base | Yes |
| `FAILED` | Unrecoverable operational failure (not an evidence or security outcome) | Yes |

`INSUFFICIENT_EVIDENCE` is terminal for the task **as scoped**. It is not automatically resumable — see [Resumability](#resumability).

## Transition table

| From | To | Trigger | Guard |
|---|---|---|---|
| `CREATED` | `PLANNING` | Orchestrator picks up task | — |
| `PLANNING` | `NEEDS_CLARIFICATION` | Ambiguity detector flags unresolved scope | At least one required scope dimension (company, metric/topic, time period, source set) is unresolved |
| `PLANNING` | `GATHERING_EVIDENCE` | Plan constructed, no ambiguity flagged | Scope fully resolved |
| `NEEDS_CLARIFICATION` | `PLANNING` | Analyst submits clarification answer | Task state (question, partial plan) is loaded from persistence, not re-created |
| `GATHERING_EVIDENCE` | `SYNTHESISING` | Planned tool calls complete | At least one evidence item retrieved, or planner explicitly allows zero-evidence synthesis (rare; empty retrieval is a listed failure-injection case — see [docs/ROADMAP.md](ROADMAP.md#failure-injection-cases)) |
| `GATHERING_EVIDENCE` | `FAILED` | Tool layer exhausts retries on a required tool call | e.g. document store unavailable, tool timeout budget exhausted |
| `SYNTHESISING` | `VALIDATING` | Draft claims produced | Every draft claim has at least a proposed `evidence_ids[]` link (possibly empty, which validation will catch) |
| `SYNTHESISING` | `SECURITY_BLOCKED` | Model-routing/DLP policy check before a claim-drafting model call returns `block` or `require_approval` | See [docs/DATA_CLASSIFICATION.md](DATA_CLASSIFICATION.md). **Phase 4 addition, not in the original table** — recorded in [docs/ROADMAP.md](ROADMAP.md#recording-deviations): the check must run *before* the model call that drafts a claim, which happens while the task is still `SYNTHESISING`, not after it reaches `VALIDATING` |
| `VALIDATING` | `INSUFFICIENT_EVIDENCE` | Sufficiency gate fails | One or more claims required for the task have `support_status` in `{UNSUPPORTED, INSUFFICIENT_EVIDENCE}` and no fallback claim set satisfies the task |
| `VALIDATING` | `SECURITY_BLOCKED` | DLP/security check on validated content returns `block` | See [docs/DATA_CLASSIFICATION.md](DATA_CLASSIFICATION.md). As of Phase 4, this row remains unreached by any implemented code path — the one enforcement call site (claim drafting) blocks earlier, from `SYNTHESISING` (see the row above). Kept for a future check on content leaving the system boundary at/after validation, not yet built |
| `VALIDATING` | `AWAITING_REVIEW` | Sufficiency gate and security check both pass | All required claims meet the validation policy in [docs/CLAIM_EVIDENCE_MODEL.md](CLAIM_EVIDENCE_MODEL.md#publication-gate); security check result is `allow` (or `redact` applied and re-checked) |
| `INSUFFICIENT_EVIDENCE` | `GATHERING_EVIDENCE` | Analyst explicitly expands scope (new sources, wider date range) on the same task | Requires a new, explicit analyst action — never automatic |
| `AWAITING_REVIEW` | `APPROVED` | Reviewer approves | Reviewer is authorized and separated from the author; see [reviewer separation](CLAIM_EVIDENCE_MODEL.md#reviewer-separation) and [identity scope](CLAIM_EVIDENCE_MODEL.md#identity-scope-portfolio-implementation) |
| `AWAITING_REVIEW` | `REVISION_REQUESTED` | Reviewer requests changes | Reviewer records reason(s); same guards as `APPROVED` above |
| `AWAITING_REVIEW` | `REJECTED` | Reviewer rejects on the merits | Same guards as `APPROVED` above. **Phase 5 addition** — see [docs/ROADMAP.md](ROADMAP.md#recording-deviations) |
| `REVISION_REQUESTED` | `GATHERING_EVIDENCE` | Reviewer flagged missing/wrong evidence; analyst resumes via `resume_from_revision(needs_new_evidence=True)` | Existing claims/evidence retained; new retrieval augments, does not discard, prior state; the *same* `task_id` continues |
| `REVISION_REQUESTED` | `SYNTHESISING` | Reviewer flagged drafting/wording issues only, evidence was adequate; analyst resumes via `resume_from_revision()` | Existing evidence retained; the *same* `task_id` continues |
| `APPROVED` | `PUBLISHED` | Publication gate re-checked and passes at publish time | See [publication gate](CLAIM_EVIDENCE_MODEL.md#publication-gate) — re-checked, not assumed from the `APPROVED` transition alone; as of Phase 5, this includes reviewer authorization and a `claim_set_version` match against the approval that was recorded |
| any non-terminal state | `FAILED` | Unrecoverable operational error not covered above | Model provider unavailable with no fallback, malformed tool output that cannot be safely interpreted, etc. |

## Invariants

- **No state transitions directly to `PUBLISHED` except `APPROVED`.** There is no shortcut from `VALIDATING`, `AWAITING_REVIEW`, or anywhere else.
- **No state transitions to `APPROVED` except `AWAITING_REVIEW`.** `SECURITY_BLOCKED` and `INSUFFICIENT_EVIDENCE` cannot be approved around; they must resolve back through evidence gathering and validation first (or, for `SECURITY_BLOCKED`, not at all within this task — see below).
- **`SECURITY_BLOCKED` is terminal for the task.** It is not analyst-resumable. If a compliant path genuinely exists (e.g. an approved exception), that is a new task created under the applicable policy, not a resume of the blocked one — this keeps a security block from ever being routed around by retrying the same task. **Phase 5 confirms this holds through the review workflow too**: `submit_review()` requires `state == AWAITING_REVIEW`, which a `SECURITY_BLOCKED` task can never be, so no review decision — however senior the reviewer — can move a security-blocked task anywhere. Resolving a `require_approval` `SecurityEvent` (`resolve_security_event()`, Phase 5) records governance sign-off on that specific event but does not touch task state, for the same reason.
- **`REJECTED` is terminal for the task, symmetrically with `SECURITY_BLOCKED`.** A reviewer's hard rejection has no built-in path back to revision within the same task; if the research is to be redone, that is a new task. (This differs from `REVISION_REQUESTED`, which is explicitly resumable — see below.)
- **The publication gate is re-evaluated at the `APPROVED` → `PUBLISHED` transition**, not only when the task first reaches `AWAITING_REVIEW`. State can be stale by the time a reviewer acts; the gate is re-checked against current persisted claim/security state, not the state at the time review started. **Phase 5**: this now includes checking that the approval on record was made against the claim set's *current* `claim_set_version` — an approval recorded against an earlier version does not authorize a version the claim set has since become (see [docs/CLAIM_EVIDENCE_MODEL.md](CLAIM_EVIDENCE_MODEL.md#version-integrity-phase-5)).

## Resumability

Two states are explicitly designed to pause and resume the *same* task rather than restart:

- **`NEEDS_CLARIFICATION`**: the task's question, any partial plan already constructed, and the specific ambiguity being asked about are persisted. When the analyst answers, the orchestrator loads that persisted state and resumes `PLANNING` — it does not re-run ambiguity detection from a blank task, and it does not lose plan fragments already agreed on (e.g. if only the time period was ambiguous, the already-resolved company scope is not re-asked).
- **`REVISION_REQUESTED`**: existing evidence and prior draft claims are retained. A revision does not restart retrieval from zero; it augments or re-synthesises from what's already gathered, unless the reviewer's feedback specifically requires new evidence (in which case only the additional retrieval is new work).

### `INSUFFICIENT_EVIDENCE` is deliberately not in the same category

`INSUFFICIENT_EVIDENCE` is **not** automatically resumable — see the invariant above. Automatically retrying evidence gathering after an insufficiency finding would create pressure to keep retrying until *something* looks supported, which is the exact failure mode this state exists to prevent. Expanding scope is allowed, but only as an explicit, logged analyst decision (`INSUFFICIENT_EVIDENCE` → `GATHERING_EVIDENCE` in the [transition table](#transition-table) above).

## What the model is and is not allowed to do to state

Per the design principle in [docs/ARCHITECTURE.md](ARCHITECTURE.md), the model may *propose* work (a plan, a draft claim, a request for clarification, a request for review) that the orchestrator turns into a transition. The model never calls the state-transition function directly, and no prompt instruction can substitute for the guards in the table above — a model saying "this claim is now verified" does not change `support_status`; only the validation engine does that, and a model saying "publish this" does not change `final_status`; only the review/approval service and the publication gate do.
