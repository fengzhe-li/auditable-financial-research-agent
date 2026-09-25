"""The three Phase 6 benchmark configurations - docs/EVALUATION_PLAN.md's
"First evaluation pass: A, D, F only."

Each run_configuration_* function takes one BenchmarkTask and returns one
TaskRunResult: a plain, JSON-serialisable record of what actually happened,
independent of grading (afra.benchmark.grader reads TaskRunResult, never
the other way around - the harness doesn't know what "correct" looks like).

Configuration A (single-shot baseline) deliberately does NOT go through
ResearchTaskOrchestrator, Repository, or afra.policy.enforcement at all -
it is a separate, minimal code path, on purpose. That is the actual
distinguishing property of a "GPT wrapper" baseline: no agent scaffolding,
no validator, no policy layer, not "the same pipeline with some steps
skipped".

Configurations D and F both go through ResearchTaskOrchestrator - Phase 4's
policy enforcement is unconditionally wired into the shared claim-drafting
path and preserving that invariant (not weakening it to make a cleaner
ablation) was an explicit Phase 6 instruction. The real distinguishing
properties between D and F that this codebase *can* honestly express are:

- D calls the orchestrator's lower-level methods directly and stops after
  run_validation() - it never calls evaluate_sufficiency_and_advance(), so
  it has no abstention/sufficiency gate (matching
  docs/EVALUATION_PLAN.md's original "D = C + validator", "E = D +
  abstention" ordering - abstention was never part of D). D's "answer" is
  whatever claims got drafted and validated, unfiltered by any sufficiency
  check, and it never reaches human review or publication.
- F runs the same steps as D, then additionally calls
  evaluate_sufficiency_and_advance() and, if that reaches AWAITING_REVIEW,
  submit_review()/publish() - the full pipeline through to an authorized,
  published output (see _run_interpret_route_draft_validate, shared by
  both configurations).

For conflicting_evidence-category tasks, BenchmarkTask.manual_conflicting_claim
bypasses the normal per-subquestion routing and mirrors the exact manual
evidence+claim construction already established in
tests/test_phase3_end_to_end.py - see that field's docstring in
afra.benchmark.schema for why: afra.routing.router.pick_latest_document only
ever retrieves the latest filing per company, so a genuine same-claim,
cross-year contradiction cannot arise from unmodified autonomous routing.
- D's benchmark task scope excludes the sensitive_data_routing_policy
  category (see afra.benchmark.schema.CONFIGURATION_D_EXCLUDED_CATEGORIES)
  because Phase 4 enforcement cannot be structurally disabled for D without
  weakening a real invariant - see docs/PHASE_6_REPORT.md for the full
  reasoning. D IS run against prompt_injection tasks, because injection
  resistance in this codebase is architectural (the deterministic
  providers never "obey" retrieved text - see
  afra/providers/test_double.py), not specific to the Phase 4 policy layer,
  so D and F are expected to behave identically there - a real, meaningful
  (if not novel) finding, not a gap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from afra.benchmark.baseline_provider import SingleShotBaselineProvider
from afra.benchmark.schema import BenchmarkTask
from afra.domain.enums import ProviderClass, ReviewDecision, SupportContribution, TaskState
from afra.domain.errors import AfraError
from afra.orchestrator.orchestrator import EvidenceLinkSpec, ResearchTaskOrchestrator
from afra.providers.base import ModelProvider
from afra.providers.test_double import (
    EnterpriseApprovedTestDoubleProvider,
    PrivateLocalTestDoubleProvider,
    TestDoubleProvider,
)
from afra.storage.repository import Repository
from afra.tools.fixtures import list_fixture_document_ids, get_fixture_document


@dataclass
class TaskRunResult:
    task_id: str
    configuration: str  # "A" / "D" / "F"
    research_task_id: str | None
    final_state: str  # a TaskState value, or "N/A" for configuration A
    answer_text: str | None  # configuration A only
    claims: list[dict] = field(default_factory=list)
    retrieved_document_ids: list[str] = field(default_factory=list)
    security_events: list[dict] = field(default_factory=list)
    model_call_count: int = 0
    tool_call_count: int = 0
    tool_call_names: list[str] = field(default_factory=list)
    total_latency_ms: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost: float = 0.0
    wall_clock_ms: float = 0.0
    error: str | None = None
    # Audit metadata only; A has no governed ResearchTask.
    policy_id: str | None = None
    policy_version: int | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def build_full_provider_registry() -> dict[ProviderClass, ModelProvider]:
    external, enterprise, private = (
        TestDoubleProvider(),
        EnterpriseApprovedTestDoubleProvider(),
        PrivateLocalTestDoubleProvider(),
    )
    return {
        external.provider_class: external,
        enterprise.provider_class: enterprise,
        private.provider_class: private,
    }


def _corpus_prompt(question: str, exclude_documents: frozenset[str] = frozenset()) -> str:
    """Every fixture document is 'available' to Configuration A, regardless
    of what the task actually needs - modelling a system with no retrieval
    step and no access control, per this module's docstring. `exclude_documents`
    applies BenchmarkTask.simulate_missing_documents identically to A, so a
    "missing filing" scenario is the same underlying information constraint
    across all three configurations - see that field's docstring.
    """
    lines = []
    for document_id in list_fixture_document_ids():
        if document_id in exclude_documents:
            continue
        doc = get_fixture_document(document_id)
        text = doc.sections.get("risk_factors", "")
        lines.append(f"[{document_id}] {text}")
    corpus_text = "\n".join(lines)
    return f"Question: {question}\n\nAvailable documents:\n{corpus_text}"


def run_configuration_a(task: BenchmarkTask, provider: SingleShotBaselineProvider | None = None) -> TaskRunResult:
    provider = provider or SingleShotBaselineProvider()
    started = time.perf_counter()
    prompt = _corpus_prompt(task.question, frozenset(task.simulate_missing_documents))
    try:
        response = provider.complete(purpose="single_shot_answer", prompt=prompt)
        result = TaskRunResult(
            task_id=task.task_id,
            configuration="A",
            research_task_id=None,
            final_state="N/A",
            answer_text=response.text,
            model_call_count=1,
            total_latency_ms=response.latency_ms,
            total_tokens_in=response.tokens_in,
            total_tokens_out=response.tokens_out,
            total_cost=response.cost,
        )
    except Exception as exc:  # pragma: no cover - defensive; the deterministic provider never raises
        result = TaskRunResult(
            task_id=task.task_id, configuration="A", research_task_id=None, final_state="ERROR",
            answer_text=None, error=str(exc),
        )
    result.wall_clock_ms = (time.perf_counter() - started) * 1000
    return result


def _collect_claims(orchestrator: ResearchTaskOrchestrator, research_task_id: str) -> list[dict]:
    claims = []
    for claim in orchestrator.repository.list_claims_for_task(research_task_id):
        links = orchestrator.repository.list_links_for_claim(claim.claim_id)
        validations = orchestrator.repository.list_validation_results_for_claim(claim.claim_id)
        latest_validation = validations[-1] if validations else None
        claims.append(
            {
                "claim_id": claim.claim_id,
                "claim_text": claim.claim_text,
                "support_status": claim.support_status.value if claim.support_status else None,
                "quote_spans": [link.quote_span for link in links],
                "evidence_ids": [link.evidence_id for link in links],
                # citation_check_passed is the real validator's own
                # determination of whether the cited quote_span(s) genuinely
                # appear in the cited evidence's raw_text - reused directly
                # as this benchmark's citation-precision signal rather than
                # recomputed independently (afra.validation.validator is
                # the single source of truth for this - see
                # docs/CLAIM_EVIDENCE_MODEL.md#validation-authority).
                "citation_check_passed": latest_validation.citation_check_passed if latest_validation else None,
            }
        )
    return claims


def _apply_missing_document_simulation(
    evidence_by_subquestion: dict[str, list], exclude_documents: frozenset[str]
) -> dict[str, list]:
    """Drops any retrieved Evidence whose source_document_id is in
    exclude_documents, from a real route_and_gather_evidence() result -
    see BenchmarkTask.simulate_missing_documents.
    """
    if not exclude_documents:
        return evidence_by_subquestion
    return {
        subq: [e for e in evidence_list if e.source_document_id not in exclude_documents]
        for subq, evidence_list in evidence_by_subquestion.items()
    }


def _collect_totals(orchestrator: ResearchTaskOrchestrator, research_task_id: str, result: TaskRunResult) -> None:
    model_calls = orchestrator.repository.list_model_calls_for_task(research_task_id)
    tool_calls = orchestrator.repository.list_tool_calls_for_task(research_task_id)
    evidence = orchestrator.repository.list_evidence_for_task(research_task_id)
    security_events = orchestrator.repository.list_security_events_for_task(research_task_id)

    task = orchestrator.repository.get_task(research_task_id)
    result.policy_id, result.policy_version = task.policy_id, task.policy_version
    result.model_call_count = len(model_calls)
    result.tool_call_count = len(tool_calls)
    result.tool_call_names = sorted({tc.tool_name for tc in tool_calls})
    result.total_latency_ms = sum(c.latency_ms for c in model_calls)
    result.total_tokens_in = sum(c.tokens_in for c in model_calls)
    result.total_tokens_out = sum(c.tokens_out for c in model_calls)
    result.total_cost = sum(c.cost for c in model_calls)
    result.retrieved_document_ids = sorted({e.source_document_id for e in evidence})
    result.security_events = [
        {"classification": e.classification.value, "action_taken": e.action_taken.value}
        for e in security_events
    ]


def _run_interpret_route_draft_validate(
    orchestrator: ResearchTaskOrchestrator, task: BenchmarkTask, research_task_id: str
):
    """Shared by D and F: interpret -> (stop if NEEDS_CLARIFICATION) ->
    route + gather evidence (with BenchmarkTask.simulate_missing_documents
    applied) -> draft claims -> complete_synthesis -> run_validation.
    Returns the task's current state after this point; D stops here, F
    continues into evaluate_sufficiency_and_advance()/review/publish - see
    run_configuration_d/run_configuration_f and this module's docstring for
    why this is where the two configurations genuinely diverge.
    """
    manual = task.manual_conflicting_claim is not None or task.manual_document_override is not None
    if manual:
        # Bypasses interpret_and_route()/decompose_question() entirely
        # (Phase 1's direct PLANNING -> GATHERING_EVIDENCE transition,
        # mark_plan_complete()) rather than letting the interpreter declare
        # real subquestions that these manually-drafted claims would then
        # leave uncovered - afra.sufficiency.coverage.compute_sufficiency
        # checks per-subquestion coverage against
        # resolved_scope["subquestions"], and a manual claim's `subquestion`
        # field cannot honestly claim to cover interpreter-generated
        # subquestions it was never drafted against. Precedented: this is
        # exactly the same pattern Phase 1/2's own tests use (see
        # tests/test_end_to_end_slice.py's _run_to_awaiting_review).
        current = orchestrator.mark_plan_complete(research_task_id)
    else:
        current = orchestrator.interpret_and_route(research_task_id)
        if current.state == TaskState.NEEDS_CLARIFICATION:
            return current

    if task.manual_conflicting_claim is not None:
        spec = task.manual_conflicting_claim
        evidence_links = []
        for item in spec["evidence"]:
            evidence = orchestrator.gather_evidence_from_document(research_task_id, item["document_id"], "risk_factors")
            contribution = (
                SupportContribution.CONTRADICTS
                if item["contribution"] == "contradicts"
                else SupportContribution.SUPPORTS
            )
            evidence_links.append(EvidenceLinkSpec(evidence.evidence_id, item["quote_span"], contribution))
        orchestrator.complete_evidence_gathering(research_task_id)
        orchestrator.draft_claim(research_task_id, prompt=spec["prompt"], evidence_links=evidence_links, created_by=current.created_by)
    elif task.manual_document_override is not None:
        # Explicit retrieval of one named document - see that field's
        # docstring in afra.benchmark.schema for why: pick_latest_document
        # always resolves to whichever Contoso/Fabrikam document has the
        # latest published_at, which for this fixture corpus is never the
        # INTERNAL/CONFIDENTIAL/RESTRICTED documents.
        evidence = orchestrator.gather_evidence_from_document(research_task_id, task.manual_document_override, "risk_factors")
        orchestrator.complete_evidence_gathering(research_task_id)
        orchestrator.draft_claim_from_evidence(research_task_id, task.question, [evidence], created_by=current.created_by)
    else:
        evidence_by_subquestion = orchestrator.route_and_gather_evidence(research_task_id)
        evidence_by_subquestion = _apply_missing_document_simulation(
            evidence_by_subquestion, frozenset(task.simulate_missing_documents)
        )
        orchestrator.complete_evidence_gathering(research_task_id)
        orchestrator.draft_claims_for_subquestions(research_task_id, evidence_by_subquestion, current.created_by)

    orchestrator.complete_synthesis(research_task_id)
    orchestrator.run_validation(research_task_id)
    return orchestrator.repository.get_task(research_task_id)


def run_configuration_d(
    task: BenchmarkTask, repository: Repository, providers: dict[ProviderClass, ModelProvider] | None = None
) -> TaskRunResult:
    """`providers` defaults to build_full_provider_registry() (Phase 6's
    original, unchanged behaviour) - the parameter exists so Phase 6.5's
    real-model harness (afra.benchmark.real_model_harness) can substitute a
    single provider (afra.providers.real_model.HybridDraftClaimProvider) into
    the EXTERNAL_STANDARD slot without duplicating this function.
    """
    started = time.perf_counter()
    providers = providers or build_full_provider_registry()
    orchestrator = ResearchTaskOrchestrator(repository, providers[ProviderClass.EXTERNAL_STANDARD], providers=providers)
    research_task = orchestrator.create_task(question_text=task.question, created_by="analyst_1")
    result = TaskRunResult(
        task_id=task.task_id, configuration="D", research_task_id=research_task.task_id, final_state="",
        answer_text=None,
    )
    try:
        # Deliberately never calls evaluate_sufficiency_and_advance(),
        # submit_review(), or publish() - see this module's docstring for
        # why that's the real, honest distinction between D and F in this
        # codebase. The task is left in VALIDATING (or NEEDS_CLARIFICATION).
        current = _run_interpret_route_draft_validate(orchestrator, task, research_task.task_id)
        result.final_state = current.state.value
        result.claims = _collect_claims(orchestrator, research_task.task_id)
    except AfraError as exc:
        result.error = str(exc)
        current = orchestrator.repository.get_task(research_task.task_id)
        result.final_state = current.state.value
        result.claims = _collect_claims(orchestrator, research_task.task_id)
    _collect_totals(orchestrator, research_task.task_id, result)
    result.wall_clock_ms = (time.perf_counter() - started) * 1000
    return result


def run_configuration_f(
    task: BenchmarkTask, repository: Repository, providers: dict[ProviderClass, ModelProvider] | None = None
) -> TaskRunResult:
    """See run_configuration_d's docstring for why `providers` is injectable."""
    started = time.perf_counter()
    providers = providers or build_full_provider_registry()
    orchestrator = ResearchTaskOrchestrator(repository, providers[ProviderClass.EXTERNAL_STANDARD], providers=providers)
    research_task = orchestrator.create_task(question_text=task.question, created_by="analyst_1")
    result = TaskRunResult(
        task_id=task.task_id, configuration="F", research_task_id=research_task.task_id, final_state="",
        answer_text=None,
    )
    try:
        current = _run_interpret_route_draft_validate(orchestrator, task, research_task.task_id)
        if current.state == TaskState.VALIDATING:
            current = orchestrator.evaluate_sufficiency_and_advance(research_task.task_id)
        if current.state == TaskState.AWAITING_REVIEW:
            current = orchestrator.submit_review(
                research_task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE
            )
            current = orchestrator.publish(research_task.task_id)
        result.final_state = current.state.value
        result.claims = _collect_claims(orchestrator, research_task.task_id)
    except AfraError as exc:
        result.error = str(exc)
        current = orchestrator.repository.get_task(research_task.task_id)
        result.final_state = current.state.value
        result.claims = _collect_claims(orchestrator, research_task.task_id)
    _collect_totals(orchestrator, research_task.task_id, result)
    result.wall_clock_ms = (time.perf_counter() - started) * 1000
    return result
