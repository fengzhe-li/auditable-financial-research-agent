# Phase 3 Report

Status: **Complete.** 79/79 tests pass (`cd backend && python3 -m pytest -v`), up from 52 at the end of Phase 2.

## The Phase 3 success criterion

> A free-text research question can be interpreted, clarified if needed, autonomously routed through the bounded tool set, and either produce reviewable evidence-backed claims or safely abstain for a structured reason.

The main gap this phase closes: Phase 2 required the caller to supply `known_companies` and to manually map each subquestion to specific `retrieve_section()` calls. `run_autonomous_research(task_id)` is now the single entry point from a bare, free-text question to `NEEDS_CLARIFICATION` / `INSUFFICIENT_EVIDENCE` / `AWAITING_REVIEW` — no caller-supplied companies, no caller-supplied tool mapping.

## One successful end-to-end state path

```text
CREATED -> PLANNING -> GATHERING_EVIDENCE -> SYNTHESISING -> VALIDATING -> AWAITING_REVIEW -> APPROVED -> PUBLISHED
```

Produced by `orch.run_autonomous_research(task_id)` for *"Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI infrastructure risk."* — no companies or tool calls supplied by the caller — followed by a normal human `submit_review`/`publish`. (`backend/scripts/print_phase3_traces.py`, scenario 1.)

## One clarification path

```text
CREATED -> PLANNING -> NEEDS_CLARIFICATION -> PLANNING -> GATHERING_EVIDENCE -> SYNTHESISING -> VALIDATING -> AWAITING_REVIEW
```

For *"Compare Contoso Cloud Corp and Fabrikam Systems Inc's AI exposure."* (the canonical ambiguous phrasing — companies resolve, but no axis is named):

```text
State after first pass: NEEDS_CLARIFICATION
pending_clarification: comparison_axis: A comparison was requested but no
  specific axis (risk, capital expenditure, revenue) was specified.
```

Answering with `{"comparison_axis": "risk"}` resumes the **same persisted task** (same `task_id`) and — because the scope is now fully resolved — automatically continues the entire rest of the pipeline through to `AWAITING_REVIEW` in the same call, with no further manual steps. (Scenario 2.)

## One abstention path

Constructed by taking a real routing result and removing one company's evidence everywhere it appears (reproducing what a real search miss would look like — see `backend/scripts/print_phase3_traces.py` scenario 3 for why the fixture corpus can't produce this gap on its own, since every company the interpreter can name already has documents):

```text
Final state: INSUFFICIENT_EVIDENCE
abstention_reason: 1 subquestion(s) have no evidence-backed claim; no
  evidence found for: Fabrikam Systems Inc; 1 claim(s) have insufficient
  evidence
```

```json
{
  "subquestions_covered": {
    "What does Contoso Cloud Corp disclose about AI infrastructure investment risk?": true,
    "What does Fabrikam Systems Inc disclose about AI infrastructure investment risk?": false,
    "How does Contoso Cloud Corp's disclosure compare to Fabrikam Systems Inc's across its two most recent filings?": true
  },
  "claims_without_evidence": ["claim_5c16a1e1b74f"],
  "unresolved_contradictions": [],
  "unsupported_claims": [],
  "insufficient_evidence_claims": ["claim_5c16a1e1b74f"],
  "comparison_sides_represented": false,
  "missing_sides": ["Fabrikam Systems Inc"],
  "is_sufficient": false
}
```

Note the third subquestion (the comparison one) shows `true` for coverage — it still has Contoso evidence linked — while `comparison_sides_represented` independently and correctly catches that Fabrikam is missing. These are deliberately two different checks; a claim can be "covered" (has *some* evidence) while a comparison is still one-sided.

## One persisted routing trace

From the same successful run as the state path above — `assemble_trace(task_id, repository)["routing_decisions"]`, a filtered view of `ToolCall` rows with `tool_name="route_subquestion"`:

```json
[
  {
    "subquestion": "What does Contoso Cloud Corp disclose about AI infrastructure investment risk?",
    "decision": "target_companies=['Contoso Cloud Corp'] rationale='subquestion explicitly names: Contoso Cloud Corp' evidence_ids=['evidence_113060c6150c']"
  },
  {
    "subquestion": "What does Fabrikam Systems Inc disclose about AI infrastructure investment risk?",
    "decision": "target_companies=['Fabrikam Systems Inc'] rationale='subquestion explicitly names: Fabrikam Systems Inc' evidence_ids=['evidence_bff0822243e1']"
  },
  {
    "subquestion": "How does Contoso Cloud Corp's disclosure compare to Fabrikam Systems Inc's across its two most recent filings?",
    "decision": "target_companies=['Contoso Cloud Corp', 'Fabrikam Systems Inc'] rationale='subquestion explicitly names: Contoso Cloud Corp, Fabrikam Systems Inc' evidence_ids=['evidence_e6fc6640a97e', 'evidence_4fb1...']"
  }
]
```

Every routing decision names its rationale in the persisted record itself — not just in a docstring.

## What's real

- **The interpreter** (`afra/interpretation/interpreter.py`): a real call through the provider abstraction, real field-by-field classification (`RESOLVED`/`AMBIGUOUS`/`MISSING`), grounded against the actual document corpus (via `search_documents()`, not a hardcoded list) so it can never "recognise" a company the system has no data for.
- **The `ResearchScope` model and its persistence**: real, JSON-serialised, round-tripped through clarification without data loss (already-resolved fields survive a clarification round-trip on a *different* field — proven the same way Phase 1 proved this for the old free-form dict).
- **The router** (`afra/routing/router.py`): real decision logic (which companies a subquestion targets, and why), executed for real against the real tool layer, with every decision persisted with its rationale.
- **The sufficiency check** (`afra/sufficiency/coverage.py`): a real, richer computation — per-subquestion coverage, comparison-side representation, unresolved contradictions — replacing Phase 1/2's bare per-claim check, and it is what now decides `INSUFFICIENT_EVIDENCE` vs. `AWAITING_REVIEW` autonomously.
- **The full autonomous pipeline** end to end, for a genuine cross-document, two-company comparison, with zero caller-supplied companies or tool-call mapping.
- **The invariant that abstention states are never chosen by the drafting model**: structurally true — `NEEDS_CLARIFICATION` is decided by `materially_unresolved_fields()` reading the interpreter's *classification* output (not its prose), and `INSUFFICIENT_EVIDENCE`/`AWAITING_REVIEW` are decided by `compute_sufficiency()` reading persisted `ValidationResult`/`ClaimEvidenceLink` rows. The claim-drafting model never sees, and cannot influence, either decision.

## What's fixture / test-double based

- **The fixture corpus is unchanged from Phase 2** — 4 documents, 2 fictional companies, 2 filing years each.
- **The interpreter's grounding is real, but its "understanding" is pattern matching.** `TestDoubleProvider._interpret()` extracts a company/axis vocabulary that's *embedded in its own prompt* and does case-insensitive substring matching against the question text. It correctly identifies "Nvidia and AMD" as unresolvable (they're not in the vocabulary) — but that's because they're absent from a fixed list, not because it understands what a company name is. A real provider adapter would still need *some* grounding mechanism; this one just doesn't attempt open-ended entity recognition.
- **The router's company-to-subquestion mapping is exact substring matching**, same caveat as Phase 2's `search_documents()`.
- **`decide_target_companies()`'s fallback rule** ("subquestion doesn't name a company → target all known companies") is untested against a genuinely ambiguous multi-company corpus, because the fixture corpus only ever has 2 companies — with 2, "all known companies" and "the comparison" are the same set. This would need a 3+ company corpus to meaningfully exercise.
- **`AXIS_TO_SECTION` is a degenerate mapping** — every known axis maps to the same `"risk_factors"` section, because that's the only section any fixture document has. The mapping exists and is real code, but nothing currently distinguishes it from a hardcoded constant.

## What remains deferred

Unchanged from Phase 2's list except: ambiguity detection is now real (this phase's core addition); autonomous tool-call selection is now real for the 3-tool scope Phase 2 implements. Still deferred: the remaining 6 tools, a real external model provider, generalising the validator beyond one quote-span check, DLP/classification enforcement, the frontend, Docker, the benchmark. See [docs/ROADMAP.md](ROADMAP.md) Phase 4 for where these land.

## Overengineering flags

1. **`AXIS_TO_SECTION` and the 3+ company fallback path are real code with no real test signal.** Both exist because the requirements named them explicitly (axis-aware routing, a fallback rule for company-less subquestions), but the current 1-section, 2-company fixture corpus can't actually distinguish "this mapping works" from "this mapping is a no-op." I built them because skipping a named requirement felt worse than a slightly undertested one — but flagging this honestly rather than presenting the test suite as proving more than it does. Expanding the fixture corpus specifically to exercise these would be the right move *if* a concrete Phase 4+ task needs it, not preemptively.
2. **The `ResearchScope` field list (6 fields) is larger than what Phase 3 actually resolves (2 fields).** Four fields exist in the data model, are persisted, and appear in every trace, while being permanently hardcoded to one default value each. This is disclosed plainly (see "What's real" above and the scope module's docstring) rather than hidden, but it's worth naming as a place where the documented data model is currently wider than the interpretation behind it — the risk is someone reading `ResearchScope`'s shape and assuming `time_range`/`document_types`/`source_constraints`/`source_scope` are meaningfully interpreted today, when they are not.
3. **Two orchestration entry points now exist for evidence-gathering** (`run_planning`+manual tool calls from Phase 2, and `interpret_and_route`+`route_and_gather_evidence` from Phase 3), both still live because Phase 1/2 tests depend on the former. This is the intended, disclosed kind of accretion (nothing was broken to add the new path), but if Phase 4 needs yet another variant, that's the point to collapse these rather than add a third.

None of the above blocks Phase 4.

## How to run it

```bash
cd backend
python3 -m pip install -e ".[dev]"
python3 -m pytest -v                          # 79 tests
python3 scripts/print_phase3_traces.py        # regenerate all three paths + the routing trace above
```

Still no network access, API key, Docker, or Postgres server required.
