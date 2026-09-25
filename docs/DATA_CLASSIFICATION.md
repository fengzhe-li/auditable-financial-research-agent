# Data Classification & AI Security Policy

Every document, chunk, evidence object, and research input in the system carries a classification. Classification determines which model provider is even allowed to process it, and what the DLP layer does with sensitive content inside it. This document defines the semantics; [docs/ARCHITECTURE.md](ARCHITECTURE.md#data-classification--model-routing-policy) shows where this sits in the pipeline.

## Classification levels

| Level | Meaning | Example |
|---|---|---|
| `PUBLIC` | Freely disclosable; already public | Published annual filings, public press releases |
| `INTERNAL` | Not for external release, but broadly available inside the firm | Internal research notes referencing only public filings, general internal commentary |
| `CONFIDENTIAL` | Sensitive to the firm or a client; restricted internal distribution | Draft/unpublished figures, client-specific research context, pre-release analysis |
| `RESTRICTED` | Highest sensitivity; access itself is controlled, not just distribution | Material non-public information, regulated client data, legally privileged material |

Classification is assigned at ingestion (per document/chunk) and is **inherited, never downgraded, by anything derived from it**: evidence retrieved from a `CONFIDENTIAL` chunk is `CONFIDENTIAL`; a claim built partly from `RESTRICTED` evidence is `RESTRICTED`. A memo's overall classification is the maximum classification of any claim or evidence it contains.

## Model-routing policy

Classification constrains which model provider/endpoint may process the data:

| Classification | Routing |
|---|---|
| `PUBLIC` | An approved external LLM provider is allowed |
| `INTERNAL` | Approved enterprise endpoint only (e.g. an enterprise agreement with data-use guarantees) — not a general public API |
| `CONFIDENTIAL` | Private/self-hosted model, or a specifically approved endpoint with contractual/technical guarantees — not a default external provider |
| `RESTRICTED` | No LLM exposure at all, unless an explicit, separately-recorded policy exception allows a specific narrow case |

This table is the *default* policy, not a hardcoded `if classification == X` scattered through the codebase.

## Policy abstraction

Routing is represented as data, not as inline conditionals:

```text
ModelRoutingRule
  classification: PUBLIC | INTERNAL | CONFIDENTIAL | RESTRICTED
  allowed_providers: [ProviderId]     # e.g. which adapters from the model provider
                                       # abstraction (docs/ARCHITECTURE.md) are permitted
  requires_approval: bool             # if true, a human must approve this specific
                                       # routing decision before the call proceeds
  notes: str
```

The policy engine loads the applicable `ModelRoutingRule` for the classification of the data involved in a proposed model call, and either permits the call against one of `allowed_providers`, requires approval first, or blocks it. This keeps the actual rule set editable/auditable (it is data a compliance reviewer can read and diff) rather than requiring a code change and redeploy to adjust routing — and it means a stricter rule set (e.g. treating a new classification, or tightening `INTERNAL` routing) is a data change, not an architecture change.

If a research task mixes classifications (e.g. some evidence is `PUBLIC`, some is `CONFIDENTIAL`), the routing decision is made **per model call** based on the classification of the data in that specific call's context, not once for the whole task — a task is allowed to use an external provider for its public-evidence claims while a self-hosted model handles the confidential ones.

## DLP / security policy actions

Independent of routing, the DLP layer inspects content for sensitive-content categories and applies one of:

| Action | Meaning |
|---|---|
| `allow` | No sensitive content detected at a level requiring action |
| `redact` | Sensitive content is removed/masked before the content proceeds (e.g. before a model call, or before inclusion in a memo) |
| `block` | The task transitions to `SECURITY_BLOCKED` (see [docs/STATE_MACHINE.md](STATE_MACHINE.md)) |
| `route_private` | Content may proceed, but only via a private/self-hosted routing path regardless of what routing would otherwise apply |
| `require_approval` | A human (compliance/governance reviewer) must explicitly approve before the content proceeds |

### Sensitive content categories

- PII
- Client identifiers
- Account numbers
- Unpublished financial figures
- Internal project names
- Confidential research notes
- Restricted attachments

**Detection is deliberately simple and deterministic**: explicit per-document/field classification, regex/pattern matching, and known sensitive-field types (e.g. an "account number" field is sensitive because of its field type, not because a model inferred it). This is a Phase 4 implementation concern, and it is intentionally **not** a machine-learning or NER project — building a state-of-the-art PII/entity detector is not this project's differentiator and is explicitly out of scope. The research/engineering question this project addresses is *policy enforcement and leakage prevention given a classification*, not *detection sophistication*. If model-assisted detection is ever added, it is itself subject to the routing policy above (detecting sensitive content does not require exposing it to an unapproved model) — but it is not assumed or required for Phase 4. This document defines the action taken once something is detected, not a commitment to a particular detection method's accuracy.

## Where this runs in the pipeline

The classification/routing check runs **before** any model call that would process the data in question — not as a post-hoc audit. The DLP check runs at two points: on data entering a model call (can this be sent here at all), and on content that would leave the system boundary (would this appear in a memo, a clarification question echoed back to the analyst, or anywhere else visible outside the task). See [docs/THREAT_MODEL.md](THREAT_MODEL.md#sensitive-data-leakage) for what this is intended to prevent, and [docs/CLAIM_EVIDENCE_MODEL.md](CLAIM_EVIDENCE_MODEL.md#securityevent) for how a policy action is recorded as an auditable event.

## Explicit non-guarantee

This document defines the *policy the application enforces*. It is not a claim that the policy is complete, that detection is perfect, or that the system is compliant with any specific regulation. See [docs/THREAT_MODEL.md](THREAT_MODEL.md) for residual risk.
