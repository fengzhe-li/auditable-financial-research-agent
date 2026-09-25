# Claim / Evidence Data Model

This is the data model the rest of the system is enforced against. If a rule matters (evidence traceability, validation, approval), it is represented here as persisted structure, not left to prompt text.

## Entity overview

```text
ResearchTask 1───* Evidence
ResearchTask 1───* Claim
Claim        *───* Evidence        via ClaimEvidenceLink
Claim        1───* ValidationResult
ResearchTask 1───* Review
ResearchTask 1───* ModelCall
ResearchTask 1───* SecurityEvent
```

## ResearchTask

The unit of work; one instance per research question (see [docs/STATE_MACHINE.md](STATE_MACHINE.md) for its lifecycle).

| Field | Notes |
|---|---|
| `task_id` | |
| `question_text` | Original analyst-submitted question |
| `resolved_scope` | Company/companies, topic/metric, time period, source set — filled in as ambiguity is resolved |
| `state` | Current state machine value |
| `classification` | Maximum classification of any evidence/claim linked to this task (derived, kept in sync as evidence is added) |
| `created_by` | Analyst |
| `created_at`, `updated_at` | |

## Evidence

A retrieved passage, with enough provenance to be cited and enough metadata to be governed.

| Field | Notes |
|---|---|
| `evidence_id` | |
| `task_id` | |
| `source_document_id` | e.g. filing identifier |
| `source_location` | Section/page/passage locator within the document |
| `source_timestamp` | When the source document was published/filed |
| `retrieved_at` | When this evidence was fetched into the task |
| `retrieval_tool` | Which controlled-tool-layer call produced it |
| `content_hash` | For reproducibility — detects if the underlying source changed between retrieval and later reference |
| `classification` | Inherited from the source document/chunk; never downgraded (see [docs/DATA_CLASSIFICATION.md](DATA_CLASSIFICATION.md)) |
| `raw_text` | The retrieved passage. Always treated as untrusted data by any downstream model call — see [docs/THREAT_MODEL.md](THREAT_MODEL.md#indirect-prompt-injection) |

## Claim

A single factual assertion proposed as part of the research output.

| Field | Notes |
|---|---|
| `claim_id` | |
| `claim_text` | |
| `task_id` | |
| `evidence_ids[]` | Denormalised convenience view of this claim's `ClaimEvidenceLink` rows — see [note below](#claimevidence_ids-vs-claimevidencelink) |
| `support_status` | One of `SUPPORTED`, `PARTIALLY_SUPPORTED`, `UNSUPPORTED`, `CONFLICTING_EVIDENCE`, `INSUFFICIENT_EVIDENCE` — a **cached snapshot of the latest `ValidationResult.computed_support_status`**, not an independently-set value; see [Validation authority](#validation-authority) below |
| `support_strength` | Numeric/ordinal confidence, likewise a cached snapshot of `ValidationResult.computed_support_strength` — not a substitute for the categorical status, and not a second source of truth |
| `validator_result` | Reference to the `ValidationResult` that produced the current cached `support_status`/`support_strength` |
| `source_timestamp` | Latest `source_timestamp` among linked evidence, for temporal claims (e.g. "as of the 2025 filing") |
| `model_id` | Which model (via the provider abstraction) drafted this claim |
| `created_by` | Analyst who initiated the task (or "system" for planner-generated scaffolding claims, if any) |
| `reviewer_id` | Set once a human reviewer has acted on this claim |
| `approval_status` | Distinct from `support_status` — see [below](#support_status-vs-approval_status) |

### `claim.evidence_ids[]` vs `ClaimEvidenceLink`

**`ClaimEvidenceLink` is the authoritative source of truth for claim–evidence relationships.** `evidence_ids[]` on `Claim` is a read-oriented, denormalised convenience view derived from `ClaimEvidenceLink` rows — useful for quickly rendering "this claim cites these N pieces of evidence" in the console. It is not an independent source of truth: it is a cache, not a relation. Any validator, or any other component making a decision about a claim's evidence, **must** read from `ClaimEvidenceLink`, never from `evidence_ids[]` — treating the denormalised list as authoritative is a bug, not an acceptable shortcut, because it can silently drift from the real link records (e.g. a link deleted or its `support_contribution` changed without the array being regenerated).

### `support_status` vs `approval_status`

These are deliberately separate fields with different owners:

- `support_status` is set **only** by the Claim/Validation Engine, computed from evidence. It answers: *is this claim supported by what was retrieved?*
- `approval_status` is set **only** by the Review/Approval Service, based on a human reviewer's decision. It answers: *has an authorized person signed off on including this claim in the memo?*

A claim can be `SUPPORTED` and still not be `approved` (reviewer hasn't acted yet, or chose to exclude it for reasons outside evidence support, e.g. relevance). A claim can never be `approved` while `support_status` is anything other than `SUPPORTED` or a specifically-allowed `PARTIALLY_SUPPORTED` case per the [publication gate](#publication-gate) — the review UI should not even offer approval for a claim that fails validation, but the *enforcement* is in the gate, not the UI.

## ClaimEvidenceLink

The normalized relationship between a claim and a piece of evidence.

| Field | Notes |
|---|---|
| `link_id` | |
| `claim_id` | |
| `evidence_id` | |
| `relevance_score` | How relevant this evidence is to this claim, independent of whether it supports or contradicts it |
| `support_contribution` | `supports` / `contradicts` / `neutral` — an individual evidence item's stance, which the validator aggregates into the claim's overall `support_status` |
| `quote_span` | The specific span of `Evidence.raw_text` the claim actually relies on, for precise citation |

## ValidationResult

The output of a validation run against a claim.

| Field | Notes |
|---|---|
| `validation_id` | |
| `claim_id` | |
| `computed_support_status` | |
| `computed_support_strength` | |
| `citation_check_passed` | Whether `validate_citations()` confirmed every cited span exists and is accurately quoted |
| `conflicting_evidence_ids[]` | Populated when `support_status == CONFLICTING_EVIDENCE` |
| `missing_evidence_description` | Populated when `support_status == INSUFFICIENT_EVIDENCE` — human-readable description of what's missing, not just a status code (see [docs/STATE_MACHINE.md](STATE_MACHINE.md#insufficient_evidence-is-deliberately-not-in-the-same-category)) |
| `validator_model_id` | If the validator used a model call for entailment checking |
| `validated_at` | |

### Validation authority

**`ValidationResult` is the authoritative record of support decisions.** Every `ValidationResult` row is retained (a claim's validation history is never overwritten in place); `Claim.support_status`/`support_strength`/`validator_result` are a cached pointer to the *latest* `ValidationResult` for that claim, kept only for convenient reads. Nothing may set `Claim.support_status` directly — it is always written as a side effect of persisting a new `ValidationResult`, by the same code path, so the cache cannot drift from the authoritative record it mirrors. If a claim is re-validated (e.g. new evidence added), a new `ValidationResult` row is created and the cache is updated to point at it; the old result remains queryable as history, not deleted.

## Review

A reviewer's decision on a draft memo (and, implicitly, its claims).

| Field | Notes |
|---|---|
| `review_id` | |
| `task_id` | |
| `reviewer_id` | |
| `decision` | `approve` / `reject` / `request_revision`. **`reject` is a Phase 5 addition** — a hard rejection on the merits, distinct from `request_revision`'s resumable revision cycle; see [docs/STATE_MACHINE.md](STATE_MACHINE.md)'s `REJECTED` state |
| `comments` | The reviewer's stated reasoning for the decision. (Kept as `comments`, not renamed to a "rationale" field, to avoid breaking every existing call site - see the Phase 5 deviation entry in [docs/ROADMAP.md](ROADMAP.md#recording-deviations). Serves the same role.) |
| `claims_reviewed[]` | Which claims this review decision covers |
| `claim_set_version` | **Phase 5 addition.** The task's `claim_set_version` at the moment this review was submitted — which version was actually reviewed. See [Version integrity](#version-integrity-phase-5) below |
| `security_context` | **Phase 5 addition.** A short, deterministic summary of the task's `SecurityEvent` history at review time, for the reviewer's/auditor's record — not itself a security decision |
| `reviewed_at` | |

### Reviewer separation

For a task's *final* review decision, `reviewer_id` must not equal the task's `created_by` (the analyst). This is enforced in the Review/Approval Service, not left as a UI convention — an attempt to record a self-review is rejected at the data layer. **Phase 5 broadens this from "self-approval only" (Phase 1-4) to every decision** (`approve`, `reject`, `request_revision` alike) — separation of duties means the reviewer role is distinct from the author role regardless of what the reviewer decides; see the Phase 5 deviation entry in [docs/ROADMAP.md](ROADMAP.md#recording-deviations). Earlier, informal feedback (e.g. a peer sanity-check before formal review) is not restricted by this rule; it is the `AWAITING_REVIEW` → `{APPROVED, REJECTED, REVISION_REQUESTED}` transitions specifically that require separation.

### Reviewer authorization (Phase 5)

Distinct from reviewer separation: `reviewer_id` must also be one of a small, fixed, in-code set of recognized reviewer identities (`afra.identity.AUTHORIZED_REVIEWER_IDS`). This is checked both when a review is submitted (fails fast) and again, independently, by the publication gate at publish time (defence in depth, the same pattern the gate already uses for every other clause). It remains local/fixture-based, not an identity-provider integration — see [Identity scope](#identity-scope-portfolio-implementation) below, which this narrows slightly from its original Phase 1 wording.

### Identity scope (portfolio implementation)

`created_by` and `reviewer_id` identify **named local fixture users** — persisted local identities such as `analyst_1`, `reviewer_1`, `governance_admin` — not accounts backed by any enterprise identity provider. There is no authentication, SSO, or session management in this project's scope.

This means:

- Reviewer separation (`reviewer_id != created_by`) is a **real, enforced constraint within the application/domain model** — an attempt to record a self-review is genuinely rejected, and this is exercised directly by tests.
- It is explicitly **not** an enterprise identity/access-control integration. Nothing here should be read as, or described elsewhere in this repository as, "enterprise identity enforcement," SSO, RBAC against a directory service, or anything a compliance reviewer could mistake for a real identity boundary. Integrating a real identity provider is out of scope for this portfolio implementation and is not on the [roadmap](ROADMAP.md).
- Who is allowed to *act as* `governance_admin` versus `analyst_1` in a given environment is a deployment/configuration concern this project does not address — the domain model enforces separation between whatever identities are supplied, but does not itself verify that a caller is who they claim to be.
- **Phase 5 addendum**: the Phase 5 publication-gate spec explicitly required a "reviewer is authorized" condition distinct from separation of duties, so `afra.identity.AUTHORIZED_REVIEWER_IDS` now exists as a small, fixed, in-code allow-list of which named fixture identities may act as a reviewer at all. This is a narrow addition, still local/fixture-based and still not authentication — it does not verify a caller is who they claim to be, it only checks that the claimed identity is one of the small set of names this project recognises as a reviewer role. `afra.identity.GOVERNANCE_REVIEWER_IDS` (currently just `governance_admin`) is narrower still: the identity permitted to resolve a `require_approval` `SecurityEvent` (see [SecurityEvent](#securityevent) below).

### Version integrity (Phase 5)

An approval binds to a specific version of the claim set, not to the task in general. `ResearchTask.claim_set_version` is an integer, incremented by the orchestrator every time a claim set is finalised for a task (`complete_synthesis()` — 1 after the first synthesis pass, 2 after a revision cycle re-drafts, and so on). Each `Review` records the `claim_set_version` it actually evaluated. The [publication gate](#publication-gate) checks that the approval on record for a task was made against that task's *current* `claim_set_version`; if the claim set changed since that approval (the version has since advanced), the approval is stale and does not authorize the current version — the task must return through review again.

This is a simple monotonic counter, not a content fingerprint/hash of the claim set itself — see the overengineering note in [docs/PHASE_5_REPORT.md](PHASE_5_REPORT.md) for the tradeoff this makes.

## Claim revision semantics

Through Phase 5, a "revision" only ever meant *adding* claims: `resume_from_revision()` returns a task to `SYNTHESISING`, the same drafting calls run again, and `complete_synthesis()` bumps `claim_set_version` — but every claim ever drafted for the task, old and new, sits in the same `claims` table with no relationship recorded between them. This section adds the missing layer on top: explicit lifecycle relationships between claims, so a revision *replaces* or *withdraws* a claim rather than just accumulating another one beside it, while never deleting or silently overwriting anything.

Four terms this project uses precisely, and does not conflate:

- **Historical claims** — every `Claim` row ever persisted for a task (`Repository.list_claims_for_task()`), regardless of lifecycle status. Never deleted, always queryable — the full audit trail of what was drafted, corrected, and withdrawn, and why (see [objection history](#claim-lifecycle-status) below).
- **Effective claims** — the subset of historical claims currently "in force" (`afra.domain.claim_lifecycle.compute_effective_claims()`). This is what a reviewer approves, what `run_validation()` validates, what `compute_sufficiency()` checks coverage over, and what the [publication gate](#publication-gate) requires `SUPPORTED` — not the full history.
- **Approved version** — the `claim_set_version` recorded on the `Review` that most recently set `APPROVED` (`Review.claim_set_version`) — which version of the effective claim set a human actually signed off on.
- **Current version** — `ResearchTask.claim_set_version` right now — the version the effective claim set is actually at. [Version integrity](#version-integrity-phase-5) above already requires approved version == current version for publication; this section is what makes "current version" mean something richer than "one more claim got added."

### Claim lifecycle status

`Claim.lifecycle_status` is one of four values (`afra.domain.enums.ClaimLifecycleStatus`):

| Status | Meaning | Reached via |
|---|---|---|
| `ACTIVE` | The claim is part of the effective set. Every claim starts here. | (initial draft) |
| `REPLACED` | Directly substituted by a new claim — e.g. a reviewer-flagged factual correction, applied without a full re-synthesis pass. | `Orchestrator.replace_claim()` |
| `SUPERSEDED` | Superseded as part of a broader revision cycle's re-synthesis. Same relationship as `REPLACED` (a successor exists), reached a different way. | `Orchestrator.supersede_claim()` |
| `WITHDRAWN` | Removed from the effective set with **no** successor — e.g. the evidence turned out irrelevant. | `Orchestrator.withdraw_claim()` |

`REPLACED`/`SUPERSEDED`/`WITHDRAWN` are terminal for that claim — nothing un-replaces a claim or reactivates a withdrawn one; a fresh correction always creates a new `Claim` row instead. A replaced/superseded claim's `successor_claim_id` points at the claim that replaced it; that successor's `predecessor_claim_id` points back. Both directions are stored (not derived at read time) so a claim's place in its lineage is a single field lookup either way.

**Objection history** is a separate, softer concept: `ClaimObjection` records a reviewer's stated concern about a specific claim (`Orchestrator.object_to_claim()`) without touching that claim's `lifecycle_status` or bumping `claim_set_version` — an objection is a flag, not a revision. A claim can accumulate any number of objections over time, across multiple review cycles, and objecting to a claim that has *already* been superseded is still valid (explaining, after the fact, why it needed to be). Enforces the same reviewer-separation and reviewer-authorization rules as `submit_review()` — an objection is a reviewer action in the same sense a review decision is.

### Effective claim set

`afra.domain.claim_lifecycle.compute_effective_claims(claims)` is a pure filter: every claim with `lifecycle_status == ACTIVE`. This relies on one invariant every lifecycle-mutating method maintains: **a claim is flipped away from `ACTIVE` in the exact same operation that gives it a successor or a withdrawn reason, never separately.** So filtering on status alone is always equivalent to walking each lineage to its current tip — there is no dangling `ACTIVE` claim with an active successor to account for.

`run_validation()`, `compute_sufficiency()`, `submit_review()` (for `claims_reviewed`), and the [publication gate](#publication-gate) were all updated to operate on the effective set, not the raw historical list. For any task that has never used `replace_claim`/`supersede_claim`/`withdraw_claim`, every claim is still `ACTIVE` — this is byte-for-byte the same behaviour every phase through 5 already had, not a change in what gets validated or published for the pre-existing happy path.

### Version increments and stale-approval propagation

`replace_claim()` and `withdraw_claim()` are standalone actions — each bumps `task.claim_set_version` immediately, since the effective set changed the instant either returns. `supersede_claim()` does not bump the version itself: it is meant to be called once per re-drafted subquestion, between drafting a revision's new claims and calling `complete_synthesis()`, which performs the one version bump for that whole batch — exactly as `complete_synthesis()` already did before this section existed. Either way, the moment `claim_set_version` advances past what the latest `APPROVE` review recorded, [Version integrity](#version-integrity-phase-5)'s existing check — unmodified — makes that approval stale: no new "invalidate the approval" step was needed or added, because staleness was already defined purely in terms of the version counter this section now has more reasons to advance. Publication remains blocked (`PublicationGateError`) until a new `Review` records the current version; the task's `state` does not need to leave `APPROVED` for this to hold — the gate re-checks version equality independently of state, on every `publish()` call.

All four lifecycle-mutating methods (`replace_claim`, `supersede_claim`, `withdraw_claim`) refuse to act once the owning task is in a terminal `TaskState` (`SECURITY_BLOCKED`, `REJECTED`, `PUBLISHED`, `FAILED`) — raising `IllegalClaimRevisionError` — since a published or otherwise terminal task's claim set is no longer revisable. `object_to_claim()` is the one exception: recording an objection is pure history and is allowed regardless of task state.

### What this does not do

No claim's `claim_text`, evidence links, or validation history are ever mutated in place — `replace_claim`/`supersede_claim`/`withdraw_claim` only ever change `lifecycle_status`/`successor_claim_id`/`withdrawn_reason` on the *old* claim and create or link a new one. There is still no memo/required-claims-subset concept (see [Publication gate](#publication-gate)'s Phase 1 simplification note above) — "the effective claim set" is still every `ACTIVE` claim on the task, not a curated subset of it. Frontend surfacing of lifecycle status/predecessor-successor/objection history is exposed via the API (`GET /api/tasks/{task_id}` — each claim in `draft_claims[]` carries `lifecycle_status`, `is_effective`, `predecessor_claim_id`, `successor_claim_id`, `withdrawn_reason`, `objections[]`; the task-level response carries `effective_claim_ids[]`) but not yet built into the console's UI components themselves — a deliberately deferred, separate piece of work, not part of this data-model/API layer.

## ModelCall

An audit record of a single model invocation, regardless of which provider handled it.

| Field | Notes |
|---|---|
| `model_call_id` | |
| `task_id` | |
| `purpose` | `interpret_question` / `plan` / `draft_claim` / `assist_validation` / `draft_memo` / ... |
| `provider_id` | Which adapter (see [docs/ARCHITECTURE.md](ARCHITECTURE.md#model-provider-abstraction)) |
| `model_id` | |
| `routing_classification` | The data classification this call was authorized against, and the `ModelRoutingRule` applied (see [docs/DATA_CLASSIFICATION.md](DATA_CLASSIFICATION.md)) |
| `input_summary` | What was sent — summarised/redacted as needed rather than always storing full raw content, itself subject to the same classification rules |
| `output_summary` | |
| `latency_ms` | |
| `tokens_in`, `tokens_out` | |
| `cost` | |
| `created_at` | |

## SecurityEvent

An audit record of a DLP/security policy decision.

| Field | Notes |
|---|---|
| `security_event_id` | |
| `task_id` | |
| `trigger` | What was being checked (a model call, a memo section, a tool output, etc.) |
| `detected_categories[]` | Which sensitive-content categories were matched, if any (see [docs/DATA_CLASSIFICATION.md](DATA_CLASSIFICATION.md#sensitive-content-categories)) |
| `action_taken` | `allow` / `redact` / `block` / `route_private` / `require_approval` |
| `resolved_by` | Set for `require_approval` events once a human acts. **Phase 5 implements this for real** (`afra.orchestrator.orchestrator.resolve_security_event()`, restricted to `afra.identity.GOVERNANCE_REVIEWER_IDS`) — but note this resolves the *event* for the audit record, it does not revive a task that has already transitioned to `SECURITY_BLOCKED` (terminal, no outgoing transitions — see [docs/STATE_MACHINE.md](STATE_MACHINE.md)). A `block` action cannot be resolved at all: this project defines no human-override path for an outright block |
| `created_at` | |

`SecurityEvent` records are never deleted, even if a later event on the same task supersedes an earlier one (e.g. a `require_approval` that was subsequently approved) — the full sequence is the audit trail, not just the latest state.

## Publication gate

A `ResearchTask` may transition `APPROVED` → `PUBLISHED` only when, evaluated against current persisted state:

```text
final_status == APPROVED
AND every claim in the memo's required-claims set has support_status
    satisfying the validation policy (SUPPORTED, or PARTIALLY_SUPPORTED
    only where the memo section explicitly allows partial support and
    discloses it)
AND no unresolved SecurityEvent for this task has action_taken in
    {block, require_approval}
AND the Review that set APPROVED satisfies reviewer separation
    (reviewer_id != created_by)
AND the Review that set APPROVED has an authorized reviewer_id
    (Phase 5 - afra.identity.AUTHORIZED_REVIEWER_IDS)
AND the Review that set APPROVED has claim_set_version equal to the
    task's current claim_set_version (Phase 5 - see "Version integrity"
    above; an approval recorded against an earlier version does not
    authorize a later one)
```

This is application logic evaluated against the database, re-checked at the moment of publication (see [docs/STATE_MACHINE.md](STATE_MACHINE.md#invariants)) — not a prompt instruction, and not a check performed once and trusted thereafter.
