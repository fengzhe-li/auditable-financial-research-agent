# Project Definition

## 1. Target users

**Primary user**

- **Internal financial research analyst** at a regulated financial institution. Uses the system to accelerate document-heavy research: comparing disclosures across filings, tracking how a company's stated risk factors change over time, and assembling evidence for a claim before writing it into a memo.

**Secondary users**

- **Senior reviewer** — reviews a draft memo's claims and evidence before approval; must be a different person from the analyst who drafted it for certain actions (see [docs/CLAIM_EVIDENCE_MODEL.md](CLAIM_EVIDENCE_MODEL.md#reviewer-separation)).
- **Research manager** — consumes approved memos and audit trace summaries; cares about throughput and quality trends across the team, not individual tool calls.
- **Compliance / AI governance reviewer** — audits *how* a memo was produced, not just its content: what data classes were touched, which model saw them, whether any security policy fired, whether unsupported claims were ever exposed outside the system.

This is **not** primarily a retail customer-facing investment chatbot. See [§7](#7-why-internal-facing-rather-than-retail-customer-facing).

## 2. Core pain point

LLMs can make financial research faster, but a research team still needs to know, for any specific conclusion in a memo:

1. Is it actually supported by evidence, or is the model filling a gap from pretraining memory?
2. Was any sensitive data (client identifiers, unpublished figures, restricted attachments) exposed to a model or provider not approved to see it?
3. Was the final output reviewed and approved by an authorized person before it was treated as usable research output?

Speed without those three answers is not useful to a regulated research function — it just moves the risk from "research took too long" to "research was fast and nobody can explain why it's trustworthy."

**Core product statement:** Make AI-assisted financial research fast enough to use and auditable enough to trust.

## 3. Representative tasks

The system is designed around multi-document, evidence-heavy research questions such as:

1. *"Compare how Microsoft and Amazon describe AI infrastructure investment risks across their latest two annual filings."*
2. *"Did Meta's discussion of AI-related capital expenditure risk materially change between its 2024 and 2025 filings?"*
3. *"What evidence supports the claim that Company X's margin improvement was driven primarily by cost reduction rather than revenue mix?"*
4. *"Compare the evolution of cybersecurity risk disclosures across three annual filings and identify which conclusions are directly supported."*
5. *"Compare Nvidia and AMD's AI exposure."*

Task 5 is deliberately included as an example of an **underspecified** request. "AI exposure" could mean revenue exposure, capital expenditure, product exposure, or risk-disclosure language, over an unspecified time period and source set. The system must not silently pick one interpretation — see [docs/STATE_MACHINE.md](STATE_MACHINE.md) for the `NEEDS_CLARIFICATION` state this routes to, and the clarification questions it is expected to ask (which dimension of exposure, which time period, which source types, public filings only).

## 4. Non-goals

- **Not a retail investment chatbot.** No consumer-facing "should I buy this stock" interaction. See [§7](#7-why-internal-facing-rather-than-retail-customer-facing).
- **Not a general-purpose RAG demo.** Retrieval is one stage in a longer pipeline that includes claim validation, abstention, and security policy — it is not the product.
- **Not a system that answers from model memory.** If a task requires evidence, the model must obtain it through the controlled tool layer; it does not get to "just know" the answer.
- **Not a chat product.** A chat input may exist for entering the research question, but the primary interface is a Research Assurance Console showing plan, evidence, claims, and approval state — not a scrolling conversation.
- **Not a self-certifying system.** The model cannot mark its own claims as verified, cannot decide what data it's allowed to see, and cannot publish its own output.
- **Not a rebuild of `financial-knowledge-intelligence-platform`.** Ingestion, indexing, and retrieval infrastructure are treated as a dependency this project can call into, not a responsibility this project re-implements. See the root [README](../README.md#relationship-to-financial-knowledge-intelligence-platform).
- **Not vendor-locked to one model provider.** See [docs/ARCHITECTURE.md](ARCHITECTURE.md#model-provider-abstraction).
- **Not (at this stage) a real-time or high-throughput system.** Research tasks are expected to run in minutes, involve human review, and are not optimized for low-latency interactive chat.
- **Not, at Phase 0, a claim of production readiness, compliance certification, or security guarantee of any kind.** See [docs/THREAT_MODEL.md](THREAT_MODEL.md) for what is and is not mitigated, and at which phase.

## 5. Success metrics

Tracked once there is a system and a frozen benchmark to run it against (see [docs/EVALUATION_PLAN.md](EVALUATION_PLAN.md)); no numbers exist yet. The categories that will be measured:

- Task completion rate
- Retrieval recall against gold evidence
- Citation precision (cited evidence actually supports the cited claim)
- Claim support accuracy (computed support status matches human-labelled gold status)
- Unsupported-claim rate reaching a *draft* memo, and — the harder bar — reaching an *approved* memo (should be zero by construction; see the publication gate in [docs/CLAIM_EVIDENCE_MODEL.md](CLAIM_EVIDENCE_MODEL.md#publication-gate))
- Abstention accuracy (does the system correctly enter `INSUFFICIENT_EVIDENCE` on deliberately unanswerable tasks, and correctly *not* abstain on answerable ones)
- Clarification accuracy (does the system ask for clarification on ambiguous tasks like example 5 above, and *not* ask on unambiguous ones)
- Prompt-injection attack success rate (target: zero for the attack classes in [docs/THREAT_MODEL.md](THREAT_MODEL.md), measured, not assumed)
- Sensitive-data leakage rate (restricted-classification content reaching an unapproved model)
- Policy-block accuracy (security policy fires when it should, and does not needlessly block benign requests)
- Latency, token usage, and cost per completed task, and per ablation configuration (see [docs/EVALUATION_PLAN.md](EVALUATION_PLAN.md#baseline--ablation-configurations))

A system that is fast but has a non-zero unsupported-claim rate in *approved* memos, or any measured prompt-injection success, has failed at this project's actual goal regardless of how good its prose reads.

## 6. Why this is not a GPT wrapper

A GPT wrapper is defined here as: user question in, model completion out, nothing in between that could be independently checked. This system is deliberately structured so that removing the "wrapper" framing is a real architectural property, not a marketing claim:

- Every factual claim that reaches an approved memo has a persisted, queryable link to the specific evidence it was validated against (`ClaimEvidenceLink`) — the claim's justification exists as data, independent of the model's own explanation.
- Claim support is computed by a separate validation step against retrieved evidence, not asserted by the same model call that drafted the claim.
- Whether the model is even allowed to run against a given piece of data is decided by a policy layer that runs before model invocation, based on the data's classification — the model does not self-report whether it should have seen something.
- Publication is gated by a boolean check over persisted state (`final_status == APPROVED AND ...`), not by an instruction in the prompt.

If none of the above existed, this would just be retrieval-augmented chat with better formatting. The claim/evidence data model, the state machine, and the deterministic gates are the actual product — see [docs/ARCHITECTURE.md](ARCHITECTURE.md) and [docs/CLAIM_EVIDENCE_MODEL.md](CLAIM_EVIDENCE_MODEL.md).

## 7. Why internal-facing rather than retail-customer-facing

- The primary failure mode this project defends against — a confidently-stated but unsupported claim — is tolerable to catch *before* publication inside a firm's research workflow (a human reviewer sees `INSUFFICIENT_EVIDENCE` or a low support status and stops it), and much less tolerable *after* it has already reached a retail customer as investment content.
- Regulated research functions already have defined roles (analyst, senior reviewer, compliance) that this system's approval/review model reuses directly. A retail chatbot has no equivalent "reviewer" in the loop by default.
- Data classification and model routing exist because internal research routinely touches non-public information (draft figures, client-related context, internal notes). A retail-facing product's inputs are typically the public request itself, not a firm's internal document set — the sensitive-data problem this project is built to solve mostly doesn't exist in that shape for a retail chatbot.
- None of this rules out a retail-facing product being built on similar primitives later. It rules out this project *starting* there, because the internal, human-reviewed, document-grounded case is where "evidence-grounded and auditable" is both most necessary and most tractable to build and evaluate first.
