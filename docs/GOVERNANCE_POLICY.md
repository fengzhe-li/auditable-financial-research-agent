# Versioned Governance Policy

LLMs interpret. Tools retrieve. Deterministic systems verify. Versioned policies decide what is allowed. Humans approve.

**Versioned Governance Policy → Deterministic Enforcement** makes the existing governance assumptions explicit, persistable and auditable. This is a narrow Python data model and deterministic validator, not a general-purpose DSL, enterprise policy engine, or LLM-generated policy system. Models never create, select, edit, or approve policies.

## Data model

`GovernancePolicy` contains `policy_id`, positive integer `version`, `name`, `status` (`DRAFT`, `ACTIVE`, `SUPERSEDED`), `description`, required timezone-aware `created_at`, optional timezone-aware `effective_from`, and three rule groups:

| Group | Fields |
| --- | --- |
| Data classes (exactly PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED) | `classification`, `allowed_provider_classes`, `blocked_provider_classes`, `require_private_provider`, `require_human_approval`, `unconditional_block`, `notes` |
| Review | `human_review_required`, `separation_of_duties_required`, `publication_requires_current_version_approval`, `conflict_requires_abstention`, `notes` |
| Evidence | `minimum_evidence_per_subquestion`, `conflicting_evidence_blocks_sufficiency`, `unsupported_claims_block_publication`, `insufficient_evidence_requires_abstention`, `notes` |

All policy dataclasses are frozen. The classification mapping is copied into a read-only mapping; provider sets are frozen. Exporting JSON does not expose mutable internal state. Validation rejects unknown fields/classes/providers, malformed types (including string booleans and boolean versions), missing metadata, contradictory allow/deny or private-provider rules, and contradictory conflict rules.

## The default policy

`default` version `1` freezes the pre-existing behavior:

| Data class | Allowed providers | Additional restriction |
| --- | --- | --- |
| PUBLIC | External standard, enterprise approved, private/local | None |
| INTERNAL | Enterprise approved, private/local | No general public endpoint |
| CONFIDENTIAL | Private/local | Private provider required |
| RESTRICTED | None | Unconditional block; approval requirement does not authorize exposure |

The fixed v1 snapshot does not change when the legacy routing table changes. Tests compare its default routing semantics with that table. The existing fallback order, provider capability check, DLP scanning/redaction, and blocked-call behavior are preserved.

Review requires a human, separation from the author, and approval of the current claim-set version. Unsupported claims cannot publish. A subquestion needs at least one distinct linked evidence item, the existing nonempty-evidence requirement. No new numeric default or confidence threshold was introduced. Conflicts and insufficient evidence cause abstention under v1.

## Versioning and historical reproducibility

`Repository.save_policy(policy)` deterministically validates and inserts an immutable `(policy_id, version)`. Every duplicate identity is rejected, even for identical content. SQLite triggers prevent update, delete and replacement of persisted versions. Rule changes require a new identity/version; older specifications remain available after process restart.

`Repository.activate_policy(policy_id, version)` changes a separate singleton selection pointer. It accepts only specifications declared `ACTIVE` whose `effective_from` has arrived. Registering a version alone does not activate it. Status is immutable declaration metadata, not a mutable lifecycle field: selecting a new version does **not** rewrite the old version to `SUPERSEDED`. There is no automatic scheduled activation. The API reports the selected identity separately from each specification's status.

New orchestrated tasks bind the selected identity. `ResearchTask.policy_id` and `policy_version` persist on disk and cannot be rebound through `save_task` or SQL updates. Every task decision resolves the bound specification, never the current active pointer. Therefore changing the active version does not change in-flight or historical task rules. Missing policy references fail closed rather than falling back to a newer default.

Initialization adds policy-reference columns to existing task, review and security-event tables and seeds default v1 once. Historical rows are assigned default v1, which reproduces the rules they previously followed. Migration is additive and idempotent; no existing claim, review or task is deleted. Direct legacy `ResearchTask` construction also defaults to v1; new orchestrated tasks explicitly select the active version.

## Deterministic enforcement

- Model-call boundary: the assigned allow/deny/private/block rules constrain provider routing before `complete()`. A per-class human approval requirement prevents exposure and emits `REQUIRE_APPROVAL`; the existing terminal security-block behavior remains. There is no new model-call exception or resume mechanism.
- Review and publication: separation of duties reads the assigned review policy. The gate requires human approval for the current claim-set version and the same policy identity, rechecks effective claims and unresolved security events, and retains reviewer authorization checks.
- Evidence sufficiency: the assigned minimum controls subquestion coverage; duplicates of the same evidence ID do not count twice. Higher positive minima can be explicitly registered in new versions. Conflicts can either abstain (default) or proceed to human review when both conflict flags are false. A conflicting claim still cannot publish, even if a human approves it. Insufficient and unsupported evidence retain their existing blocking behavior.

The current state machine has no safe autonomous-approval, stale-approval, unsupported-publication or insufficient-evidence bypass. Specifications setting `human_review_required`, `publication_requires_current_version_approval`, `unsupported_claims_block_publication` or `insufficient_evidence_requires_abstention` to false are rejected rather than silently ignored. These are explicit supported-policy constraints, not promises of unimplemented modes.

Deterministic code still owns classification aggregation, DLP patterns, provider capability declarations, state transitions, evidence validation and support statuses, effective claim selection/revision semantics, reviewer fixture identities, and terminal-state behavior. The minimum-evidence rule applies to subquestion coverage, not a new universal evidence-count publication threshold.

## Audit trace and API

Task traces include the bound reference. Every routing SecurityEvent and human Review records the actual policy identity. Sufficiency results include it too. Every explicit `publish()` attempt appends a publication decision with the policy identity, claim-set version, outcome, reasons and timestamp; read-only gate previews do not create audit events. Historical attempts before this feature cannot be reconstructed as new audit events.

Read-only endpoints:

- `GET /api/policies/active`: selected specification.
- `GET /api/policies`: available versions plus selected identity.
- `GET /api/tasks/{task_id}/policy`: task-bound specification.

Task summaries/detail also expose compact references; no frontend change or policy editor was needed. Registration and activation are local repository operations, not public write endpoints. Existing task detail and trace fields remain available.

New D/F benchmark result serialization includes `policy_id` and `policy_version`; ungoverned configuration A reports null. This is additive provenance only. Frozen tasks, gold labels, grading, A/D/F control flow, historical Phase 6 artifacts, real-provider code, rate limiting, and Phase 6.5 live result directories are unchanged.

## Limitations

Policies and enforcement code must both be retained to reproduce a historical run: a policy reference alone cannot freeze future code, source evidence, or provider behavior. The database is local SQLite, with no cryptographic signing, tamper-evident store, RBAC or enterprise authentication. Reviewer identities remain a fixture allow-list. There is one deployment-wide selection pointer; explicit rollback is possible by selecting a prior ACTIVE specification and affects only new tasks. No policy editing UI, model-call exception workflow, or automatic migrations between task policies are provided.
