"""ResearchTaskOrchestrator - the only component allowed to change a task's
state (docs/STATE_MACHINE.md#invariants).

Phase 1 exercised: CREATED -> PLANNING -> NEEDS_CLARIFICATION -> (resume) ->
PLANNING -> GATHERING_EVIDENCE -> SYNTHESISING -> VALIDATING ->
{INSUFFICIENT_EVIDENCE | AWAITING_REVIEW} -> {APPROVED -> PUBLISHED |
REVISION_REQUESTED}, with claim drafting and evidence gathering both driven
by an externally-supplied prompt/tool call.

Phase 2 added: a minimal planner (decompose_question) that turns a bounded
question into 2-4 subquestions given known companies; three controlled
tools (search_documents, get_document, retrieve_section); and
evidence-driven claim drafting. The caller still had to supply
known_companies and manually map each subquestion to specific tool calls.

Phase 3 closes that gap: interpret_and_route() replaces the caller-supplied
known_companies with a real (if grounded-and-simple) interpretation step
(afra.interpretation.interpreter), and route_and_gather_evidence() replaces
manual subquestion-to-tool-call mapping with a minimal router
(afra.routing.router). run_autonomous_research() is the single entry point
that runs the whole thing from a bare free-text question to
NEEDS_CLARIFICATION / INSUFFICIENT_EVIDENCE / AWAITING_REVIEW - never
further: review and publication still always require an explicit human
action (submit_review, publish), matching "humans approve."

evaluate_sufficiency_and_advance() is also extended in Phase 3: it now
computes and persists a full sufficiency result
(afra.sufficiency.coverage.compute_sufficiency) instead of a bare
per-claim status check, and a claim with CONFLICTING_EVIDENCE now routes
the task to INSUFFICIENT_EVIDENCE rather than passing through to
AWAITING_REVIEW as it did in Phase 2 - a deliberate, recorded behaviour
change (docs/ROADMAP.md deviations) explained in docs/PHASE_3_REPORT.md.

Phase 4 adds real policy enforcement: _draft_and_persist_claim() now calls
afra.policy.enforcement.enforce_model_call_policy() before every model call,
using the maximum classification of the evidence a claim cites (or the
task's own classification for evidence-free drafting). A `block` or
`require_approval` decision transitions SYNTHESISING -> SECURITY_BLOCKED (a
new transition - see docs/ROADMAP.md's Phase 4 deviation entry) and raises
SecurityPolicyError instead of ever reaching the model provider; every
decision, allowed or not, is persisted as a SecurityEvent. The orchestrator
now holds a `providers` registry (ProviderClass -> ModelProvider) rather
than a single provider, so enforcement has more than one real option to
route between - see the constructor docstring below.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from afra.domain.enums import (
    CLASSIFICATION_RANK,
    Classification,
    ClaimLifecycleStatus,
    DLPAction,
    ProviderClass,
    ReviewDecision,
    ScopeFieldStatus,
    SupportContribution,
    TERMINAL_STATES,
    TaskState,
)
from afra.domain.claim_lifecycle import compute_effective_claims
from afra.domain.errors import (
    ClaimNotFoundError,
    IllegalClaimRevisionError,
    IllegalTransitionError,
    SecurityPolicyError,
    SelfApprovalError,
    UnauthorizedReviewerError,
)
from afra.domain.models import (
    Claim,
    ClaimEvidenceLink,
    ClaimObjection,
    Evidence,
    ModelCall,
    Review,
    ResearchTask,
    SecurityEvent,
    StateTransition,
    ToolCall,
)
from afra.identity import AUTHORIZED_REVIEWER_IDS, GOVERNANCE_REVIEWER_IDS
from afra.interpretation.interpreter import interpret_question
from afra.interpretation.scope import ResearchScope, ScopeField, materially_unresolved_fields
from afra.orchestrator.state_machine import require_legal_transition
from afra.planning.planner import decompose_question
from afra.policy.enforcement import enforce_model_call_policy
from afra.providers.base import ModelProvider
from afra.review.publication_gate import evaluate_publication_gate
from afra.routing.router import decide_target_companies, pick_latest_document, section_for_axis
from afra.storage.repository import Repository
from afra.sufficiency.coverage import compute_sufficiency
from afra.synthesis.claim_drafting import build_claim_prompt
from afra.tools.fixtures import FixtureDocument, UnknownDocumentError
from afra.tools.get_document import get_document
from afra.tools.retrieve_section import retrieve_section
from afra.tools.search_documents import search_documents as _search_documents
from afra.validation.validator import validate_claim


@dataclass(frozen=True)
class EvidenceLinkSpec:
    evidence_id: str
    quote_span: str
    support_contribution: SupportContribution


class ResearchTaskOrchestrator:
    def __init__(
        self,
        repository: Repository,
        provider: ModelProvider,
        providers: dict[ProviderClass, ModelProvider] | None = None,
    ):
        """`provider` remains the default/requested provider for drafting
        calls (and is what every Phase 1-3 call site continues to pass
        positionally, unchanged). `providers` is the Phase 4 registry
        afra.policy.enforcement selects from when the requested provider
        isn't eligible for a call's classification, or when DLP requires
        rerouting to PRIVATE_LOCAL; it defaults to a single-entry registry
        containing only `provider`, so a caller that never heard of Phase 4
        multi-provider routing keeps exactly its old (PUBLIC-only,
        single-provider) behaviour.
        """
        self.repository = repository
        self.provider = provider
        self.providers = providers or {provider.provider_class: provider}

    # -- internal ---------------------------------------------------------

    def _transition(self, task: ResearchTask, to_state: TaskState) -> ResearchTask:
        from_state = task.state
        require_legal_transition(from_state, to_state)
        task.state = to_state
        task.updated_at = datetime.now(timezone.utc)
        self.repository.save_task(task)
        self.repository.save_state_transition(
            StateTransition(task_id=task.task_id, from_state=from_state, to_state=to_state)
        )
        return task

    def _fail(self, task: ResearchTask, reason: str) -> ResearchTask:
        task.resolved_scope["failure_reason"] = reason
        return self._transition(task, TaskState.FAILED)

    def _log_tool_call(
        self, task_id: str, tool_name: str, input_summary: str, output_summary: str, succeeded: bool = True
    ) -> None:
        self.repository.save_tool_call(
            ToolCall(
                task_id=task_id,
                tool_name=tool_name,
                input_summary=input_summary[:200],
                output_summary=output_summary[:200],
                succeeded=succeeded,
            )
        )

    # -- lifecycle: creation and clarification -----------------------------

    def create_task(self, question_text: str, created_by: str) -> ResearchTask:
        policy = self.repository.get_active_policy()
        task = ResearchTask(question_text=question_text, created_by=created_by,
                            policy_id=policy.policy_id, policy_version=policy.version)
        self.repository.save_task(task)
        return self._transition(task, TaskState.PLANNING)

    def request_clarification(self, task_id: str, ambiguity_description: str) -> ResearchTask:
        task = self.repository.get_task(task_id)
        task.pending_clarification = ambiguity_description
        return self._transition(task, TaskState.NEEDS_CLARIFICATION)

    def submit_clarification_answer(self, task_id: str, resolved_scope_update: dict) -> ResearchTask:
        """Resume a task from persisted state. This method loads `task` fresh
        from the repository - it does not depend on any in-memory state from
        when request_clarification() was called, which is what makes the
        resumability test in tests/test_resumability.py meaningful: a
        process restart between the two calls changes nothing about this
        method's behaviour.

        Phase 3 addition: if this task went through interpret_and_route()
        (i.e. it has a persisted research_scope), `resolved_scope_update`
        keys matching a ResearchScope field name (companies,
        comparison_axis, ...) resolve that specific field rather than being
        merged as a raw dict value, and the task automatically continues -
        re-entering NEEDS_CLARIFICATION if scope is still incomplete, or
        running the full autonomous pipeline through to
        INSUFFICIENT_EVIDENCE/AWAITING_REVIEW if it's now complete. Tasks
        that never went through interpret_and_route() (Phase 1/2's own
        tests) keep the original raw-merge behaviour unchanged.
        """
        task = self.repository.get_task(task_id)
        task.resolved_scope.setdefault("clarification_responses", []).append(resolved_scope_update)

        has_research_scope = "research_scope" in task.resolved_scope
        if has_research_scope:
            scope = ResearchScope.from_dict(task.resolved_scope["research_scope"])
            for field_name in ResearchScope.FIELD_NAMES:
                if field_name in resolved_scope_update:
                    setattr(
                        scope,
                        field_name,
                        ScopeField(ScopeFieldStatus.RESOLVED, value=resolved_scope_update[field_name]),
                    )
            task.resolved_scope["research_scope"] = scope.to_dict()
            for key, value in resolved_scope_update.items():
                if key not in ResearchScope.FIELD_NAMES:
                    task.resolved_scope[key] = value
        else:
            task.resolved_scope.update(resolved_scope_update)

        task.pending_clarification = None
        task = self._transition(task, TaskState.PLANNING)

        if has_research_scope:
            task = self._resolve_scope_or_clarify(task_id)
            if task.state == TaskState.GATHERING_EVIDENCE:
                task = self.run_autonomous_evidence_and_synthesis(task_id)
        return task

    def mark_plan_complete(self, task_id: str) -> ResearchTask:
        """Phase 1's direct PLANNING -> GATHERING_EVIDENCE transition, kept
        for tasks that don't go through run_planning()/interpret_and_route()
        (e.g. Phase 1's own tests). Just advances the state.
        """
        task = self.repository.get_task(task_id)
        return self._transition(task, TaskState.GATHERING_EVIDENCE)

    # -- lifecycle: planning (Phase 2) --------------------------------------

    def run_planning(self, task_id: str, known_companies: list[str]) -> list[str]:
        """Phase 2's planning path: caller supplies known_companies
        directly. Still available (used by Phase 2's own tests);
        interpret_and_route() is the Phase 3 path that derives
        known_companies from the question text instead.
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.PLANNING:
            raise IllegalTransitionError(
                f"cannot plan while task is in {task.state.value}"
            )
        try:
            subquestions = decompose_question(
                task_id, task.question_text, known_companies, self.provider, self.repository
            )
        except Exception as exc:
            self._fail(task, f"planning failed: {exc}")
            raise
        task.resolved_scope["subquestions"] = subquestions
        task.resolved_scope["known_companies"] = known_companies
        self.repository.save_task(task)
        self._transition(task, TaskState.GATHERING_EVIDENCE)
        return subquestions

    # -- lifecycle: interpretation + routing (Phase 3) -----------------------

    def interpret_and_route(self, task_id: str) -> ResearchTask:
        """The Phase 3 entry point: interprets the task's free-text question
        into a ResearchScope (afra.interpretation.interpreter) and either
        enters NEEDS_CLARIFICATION (persisting exactly which fields are
        unresolved and why) or decomposes into subquestions and advances to
        GATHERING_EVIDENCE - all without the caller supplying company
        names, unlike run_planning().
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.PLANNING:
            raise IllegalTransitionError(
                f"cannot interpret while task is in {task.state.value}"
            )
        scope = interpret_question(task_id, task.question_text, self.provider, self.repository)
        task.resolved_scope["research_scope"] = scope.to_dict()
        self.repository.save_task(task)
        return self._resolve_scope_or_clarify(task_id)

    def _resolve_scope_or_clarify(self, task_id: str) -> ResearchTask:
        """Shared by interpret_and_route() and submit_clarification_answer():
        given a task with a persisted research_scope, check whether it's
        now fully resolved. If not, (re-)enter NEEDS_CLARIFICATION with an
        updated description. If so, decompose into subquestions and advance
        to GATHERING_EVIDENCE.
        """
        task = self.repository.get_task(task_id)
        scope = ResearchScope.from_dict(task.resolved_scope["research_scope"])
        unresolved = materially_unresolved_fields(scope)
        if unresolved:
            description = "; ".join(f"{name}: {field.reason}" for name, field in unresolved)
            return self.request_clarification(task_id, description)

        companies = scope.companies.value
        subquestions = decompose_question(
            task_id, task.question_text, companies, self.provider, self.repository
        )
        task.resolved_scope["subquestions"] = subquestions
        self.repository.save_task(task)
        return self._transition(task, TaskState.GATHERING_EVIDENCE)

    # -- controlled tool layer (Phase 2) -------------------------------------

    def search_documents(
        self, task_id: str, company: str | None = None, keyword: str | None = None
    ) -> list[FixtureDocument]:
        results = _search_documents(company=company, keyword=keyword)
        self._log_tool_call(
            task_id,
            "search_documents",
            input_summary=f"company={company!r} keyword={keyword!r}",
            output_summary=", ".join(d.document_id for d in results) or "(no results)",
        )
        return results

    def get_document_metadata(self, task_id: str, document_id: str) -> FixtureDocument:
        try:
            doc = get_document(document_id)
        except UnknownDocumentError as exc:
            self._log_tool_call(
                task_id, "get_document", input_summary=document_id, output_summary=str(exc), succeeded=False
            )
            raise
        self._log_tool_call(task_id, "get_document", input_summary=document_id, output_summary=doc.title)
        return doc

    # -- lifecycle: evidence gathering --------------------------------------

    def gather_evidence_from_document(
        self, task_id: str, document_id: str, section: str = "risk_factors"
    ) -> Evidence:
        """Retained name for Phase 1 compatibility; internally calls
        retrieve_section() - see docs/ROADMAP.md's recorded Phase 2
        deviation on the tool-name correction.

        Phase 6 fix: catches any tool-layer exception, not only
        (UnknownDocumentError, KeyError) - docs/STATE_MACHINE.md has always
        documented GATHERING_EVIDENCE -> FAILED for "Tool layer exhausts
        retries on a required tool call" (e.g. a timeout), but the
        implementation only ever handled the two known-document/known-section
        failure shapes; anything else (a simulated tool timeout, in
        particular - see tests/test_failure_injection.py) propagated
        uncaught and left the task stuck in GATHERING_EVIDENCE forever,
        contradicting the documented behaviour. Found via Phase 6 failure
        injection, fixed rather than left as a silent gap between
        documentation and implementation - see docs/ROADMAP.md's Phase 6
        deviation entry.
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.GATHERING_EVIDENCE:
            raise IllegalTransitionError(
                f"cannot gather evidence while task is in {task.state.value}"
            )
        try:
            evidence = retrieve_section(task_id, document_id, section)
        except Exception as exc:
            self._log_tool_call(
                task_id, "retrieve_section", input_summary=f"{document_id}#{section}",
                output_summary=str(exc), succeeded=False,
            )
            self._fail(task, f"retrieve_section failed: {exc}")
            raise
        self.repository.save_evidence(evidence)
        self._log_tool_call(
            task_id, "retrieve_section", input_summary=f"{document_id}#{section}",
            output_summary=f"evidence_id={evidence.evidence_id} chars={len(evidence.raw_text)}",
        )
        return evidence

    def complete_evidence_gathering(self, task_id: str) -> ResearchTask:
        """Zero evidence overall is allowed through (Phase 2's "allow
        zero-evidence synthesis" override) - a subquestion can genuinely
        have no relevant evidence, and the system should say so via
        INSUFFICIENT_EVIDENCE rather than block.
        """
        task = self.repository.get_task(task_id)
        return self._transition(task, TaskState.SYNTHESISING)

    # -- routing (Phase 3) ----------------------------------------------------

    def route_and_gather_evidence(self, task_id: str) -> dict[str, list[Evidence]]:
        """The Phase 3 router: for each persisted subquestion, decides which
        known companies it targets (afra.routing.router.decide_target_companies),
        searches for and picks each target company's latest filing, and
        retrieves the axis-appropriate section as Evidence. Every routing
        decision (target companies + rationale + resulting evidence ids) is
        persisted as a ToolCall (tool_name="route_subquestion") - reusing
        Phase 2's audit table rather than adding a third one, see
        docs/PHASE_3_REPORT.md.

        Requires a persisted research_scope (i.e. this task went through
        interpret_and_route(), not run_planning()).
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.GATHERING_EVIDENCE:
            raise IllegalTransitionError(
                f"cannot route while task is in {task.state.value}"
            )
        scope = ResearchScope.from_dict(task.resolved_scope["research_scope"])
        known_companies = scope.companies.value or []
        section = section_for_axis(scope.comparison_axis.value)
        subquestions: list[str] = task.resolved_scope.get("subquestions", [])

        evidence_by_subquestion: dict[str, list[Evidence]] = {}
        for subquestion in subquestions:
            target_companies, rationale = decide_target_companies(subquestion, known_companies)
            evidence_list: list[Evidence] = []
            for company in target_companies:
                candidates = self.search_documents(task_id, company=company)
                document = pick_latest_document(candidates)
                if document is None:
                    continue
                evidence = self.gather_evidence_from_document(task_id, document.document_id, section)
                evidence_list.append(evidence)
            evidence_by_subquestion[subquestion] = evidence_list
            self._log_tool_call(
                task_id,
                "route_subquestion",
                input_summary=subquestion,
                output_summary=(
                    f"target_companies={target_companies} rationale={rationale!r} "
                    f"evidence_ids={[e.evidence_id for e in evidence_list]}"
                ),
            )
        return evidence_by_subquestion

    # -- lifecycle: synthesis (claim drafting) ------------------------------

    def draft_claim(
        self,
        task_id: str,
        prompt: str,
        evidence_links: list[EvidenceLinkSpec],
        created_by: str,
    ) -> Claim:
        """Phase 1's general-purpose drafting call: caller supplies the
        prompt directly. Still available and still used by Phase 1/2's
        hand-constructed failure-case tests.
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.SYNTHESISING:
            raise IllegalTransitionError(
                f"cannot draft a claim while task is in {task.state.value}"
            )
        return self._draft_and_persist_claim(task_id, prompt, evidence_links, created_by)

    def draft_claim_from_evidence(
        self,
        task_id: str,
        subquestion: str,
        evidence_list: list[Evidence],
        created_by: str,
    ) -> Claim:
        """Phase 2's evidence-driven drafting call: the prompt is built from
        real retrieved Evidence, not supplied ad hoc by the caller. Every
        evidence item passed in is linked as SUPPORTS with its full
        raw_text as the quote_span - this drafting step does not itself
        decide support_status (it can't: that field isn't set here at all,
        only by Repository.record_validation_result() later). The
        resulting claim records `subquestion` so
        afra.sufficiency.coverage can check per-subquestion coverage.
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.SYNTHESISING:
            raise IllegalTransitionError(
                f"cannot draft a claim while task is in {task.state.value}"
            )
        prompt = build_claim_prompt(subquestion, evidence_list)
        evidence_links = [
            EvidenceLinkSpec(
                evidence_id=evidence.evidence_id,
                quote_span=evidence.raw_text,
                support_contribution=SupportContribution.SUPPORTS,
            )
            for evidence in evidence_list
        ]
        return self._draft_and_persist_claim(task_id, prompt, evidence_links, created_by, subquestion=subquestion)

    def draft_claim_without_evidence(self, task_id: str, subquestion: str, created_by: str) -> Claim:
        """For a subquestion that routing found nothing relevant for.
        Deliberately does NOT call the model provider - this is a
        system-generated placeholder, not a model output. The resulting
        Claim has zero ClaimEvidenceLink rows, which the validator already
        resolves to INSUFFICIENT_EVIDENCE with no new validator logic.
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.SYNTHESISING:
            raise IllegalTransitionError(
                f"cannot draft a claim while task is in {task.state.value}"
            )
        claim = Claim(
            task_id=task_id,
            claim_text=f"No evidence was found addressing: {subquestion}",
            model_id="none (no retrieval match)",
            created_by=created_by,
            subquestion=subquestion,
            # The version this claim is pending becoming part of - see
            # Claim.claim_set_version's docstring and complete_synthesis().
            claim_set_version=task.claim_set_version + 1,
        )
        self.repository.save_claim(claim)
        return claim

    def draft_claims_for_subquestions(
        self, task_id: str, evidence_by_subquestion: dict[str, list[Evidence]], created_by: str
    ) -> list[Claim]:
        """Drafts one claim per subquestion: evidence-driven where routing
        found evidence, a no-evidence placeholder otherwise. This is where
        an evidence-coverage gap becomes visible as a real, persisted claim
        rather than something the caller has to special-case.
        """
        claims = []
        for subquestion, evidence_list in evidence_by_subquestion.items():
            if evidence_list:
                claim = self.draft_claim_from_evidence(task_id, subquestion, evidence_list, created_by)
            else:
                claim = self.draft_claim_without_evidence(task_id, subquestion, created_by)
            claims.append(claim)
        return claims

    def _classification_for_evidence_links(
        self, task: ResearchTask, evidence_links: list[EvidenceLinkSpec]
    ) -> Classification:
        """The classification a proposed claim-drafting call must be
        enforced against: the maximum classification of every evidence item
        it cites (classification is inherited, never downgraded, by
        anything derived from it - docs/DATA_CLASSIFICATION.md), or the
        task's own classification for evidence-free drafting (e.g.
        draft_claim() called with evidence_links=[]).
        """
        if not evidence_links:
            return task.classification
        classifications = [
            self.repository.get_evidence(spec.evidence_id).classification for spec in evidence_links
        ]
        return max(classifications, key=lambda c: CLASSIFICATION_RANK[c])

    def _draft_and_persist_claim(
        self,
        task_id: str,
        prompt: str,
        evidence_links: list[EvidenceLinkSpec],
        created_by: str,
        subquestion: str | None = None,
    ) -> Claim:
        task = self.repository.get_task(task_id)
        classification = self._classification_for_evidence_links(task, evidence_links)
        decision = enforce_model_call_policy(
            classification=classification,
            prompt=prompt,
            requested_provider_class=self.provider.provider_class,
            providers=self.providers,
            policy=self.repository.policy_for_task(task_id),
        )
        selected_provider = (
            self.providers.get(decision.selected_provider_class) if decision.selected_provider_class else None
        )
        self.repository.save_security_event(
            SecurityEvent(
                task_id=task_id,
                trigger="draft_claim:model_routing",
                classification=classification,
                action_taken=decision.action,
                detected_categories=decision.detected_categories,
                requested_provider_id=self.provider.provider_id,
                selected_provider_id=selected_provider.provider_id if selected_provider else None,
                policy_result=decision.reason,
                policy_id=decision.policy_id,
                policy_version=decision.policy_version,
            )
        )
        if not decision.allowed or selected_provider is None:
            self._transition(task, TaskState.SECURITY_BLOCKED)
            raise SecurityPolicyError(decision.reason)

        response = selected_provider.complete(purpose="draft_claim", prompt=decision.prompt)
        self.repository.save_model_call(
            ModelCall(
                task_id=task_id,
                purpose="draft_claim",
                provider_id=selected_provider.provider_id,
                model_id=selected_provider.model_id,
                routing_classification=classification,
                input_summary=decision.prompt[:200],
                output_summary=response.text[:200],
                latency_ms=response.latency_ms,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost=response.cost,
            )
        )

        claim = Claim(
            task_id=task_id,
            claim_text=response.text,
            model_id=selected_provider.model_id,
            created_by=created_by,
            subquestion=subquestion,
            # The version this claim is pending becoming part of - see
            # Claim.claim_set_version's docstring and complete_synthesis().
            claim_set_version=task.claim_set_version + 1,
        )
        self.repository.save_claim(claim)

        for spec in evidence_links:
            self.repository.save_link(
                ClaimEvidenceLink(
                    claim_id=claim.claim_id,
                    evidence_id=spec.evidence_id,
                    quote_span=spec.quote_span,
                    support_contribution=spec.support_contribution,
                )
            )

        return claim

    def complete_synthesis(self, task_id: str) -> ResearchTask:
        """Phase 5: increments task.claim_set_version every time a claim set
        is finalised for this task - 1 after the first synthesis pass, 2
        after a revision cycle re-drafts, and so on. A Review's own
        claim_set_version (see submit_review) records which version it
        actually evaluated, so the publication gate can detect a stale
        approval - docs/CLAIM_EVIDENCE_MODEL.md's version-integrity note.
        """
        task = self.repository.get_task(task_id)
        if not self.repository.list_claims_for_task(task_id):
            raise ValueError("cannot leave SYNTHESISING with zero claims drafted")
        task.claim_set_version += 1
        return self._transition(task, TaskState.VALIDATING)

    # -- lifecycle: validation and sufficiency ------------------------------

    def run_validation(self, task_id: str) -> list:
        """Validates the task's currently *effective* claims only
        (afra.domain.claim_lifecycle.compute_effective_claims) - a claim
        already REPLACED/SUPERSEDED/WITHDRAWN by an earlier revision is
        history, not something publication depends on, so re-validating it
        on every subsequent pass would be both wasted work and (for
        sufficiency/publication purposes) actively misleading. Before any
        revision-semantics action has ever been used on a task, every claim
        is still ACTIVE, so this is identical to validating every claim -
        unchanged behaviour for every pre-existing call path.
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.VALIDATING:
            raise IllegalTransitionError(
                f"cannot validate while task is in {task.state.value}"
            )
        results = []
        effective_claims = compute_effective_claims(self.repository.list_claims_for_task(task_id))
        for claim in effective_claims:
            result = validate_claim(claim.claim_id, self.repository)
            self.repository.record_validation_result(result)
            results.append(result)
        return results

    def evaluate_sufficiency_and_advance(self, task_id: str) -> ResearchTask:
        """VALIDATING -> INSUFFICIENT_EVIDENCE | AWAITING_REVIEW.

        Phase 3: computes and persists the full sufficiency result
        (afra.sufficiency.coverage.compute_sufficiency) to
        resolved_scope["sufficiency_result"], and sets
        resolved_scope["abstention_reason"] when the task abstains. A claim
        with CONFLICTING_EVIDENCE now blocks sufficiency (routes to
        INSUFFICIENT_EVIDENCE) - in Phase 2 it passed through to
        AWAITING_REVIEW and was only caught later by the publication gate.
        This is a deliberate, recorded behaviour change: an autonomously
        discovered, unresolved contradiction should not be handed to a
        reviewer as if it were a normal draft - see
        docs/ROADMAP.md#recording-deviations and docs/PHASE_3_REPORT.md.

        VALIDATING -> SECURITY_BLOCKED (this method's own transition set)
        remains unreached in Phase 4: policy enforcement now runs earlier,
        during SYNTHESISING (see _draft_and_persist_claim), which is where a
        block actually happens for the one call site that exists so far -
        see docs/PHASE_4_REPORT.md.
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.VALIDATING:
            raise IllegalTransitionError(
                f"cannot evaluate sufficiency while task is in {task.state.value}"
            )

        sufficiency = compute_sufficiency(task, self.repository)
        task.resolved_scope["sufficiency_result"] = sufficiency

        if not sufficiency["is_sufficient"]:
            task.resolved_scope["abstention_reason"] = sufficiency["reason"]
            self.repository.save_task(task)
            return self._transition(task, TaskState.INSUFFICIENT_EVIDENCE)

        task.resolved_scope.pop("abstention_reason", None)
        self.repository.save_task(task)
        return self._transition(task, TaskState.AWAITING_REVIEW)

    def expand_scope_after_insufficient_evidence(self, task_id: str) -> ResearchTask:
        """The one explicit, analyst-triggered resume path out of
        INSUFFICIENT_EVIDENCE - see
        docs/STATE_MACHINE.md#insufficient_evidence-is-deliberately-not-in-the-same-category.
        Never called automatically.
        """
        task = self.repository.get_task(task_id)
        return self._transition(task, TaskState.GATHERING_EVIDENCE)

    # -- autonomous pipeline (Phase 3) -----------------------------------------

    def run_autonomous_evidence_and_synthesis(self, task_id: str) -> ResearchTask:
        """From GATHERING_EVIDENCE: autonomously routes each subquestion to
        evidence, drafts claims, validates, and evaluates sufficiency,
        returning the task in whichever state that lands it in
        (INSUFFICIENT_EVIDENCE or AWAITING_REVIEW). No human input is used
        for any of these steps, and no state here is chosen by the drafting
        model - see evaluate_sufficiency_and_advance and
        afra.validation.validator for where each status actually comes
        from.
        """
        task = self.repository.get_task(task_id)
        evidence_by_subquestion = self.route_and_gather_evidence(task_id)
        self.complete_evidence_gathering(task_id)
        self.draft_claims_for_subquestions(task_id, evidence_by_subquestion, task.created_by)
        self.complete_synthesis(task_id)
        self.run_validation(task_id)
        return self.evaluate_sufficiency_and_advance(task_id)

    def run_autonomous_research(self, task_id: str) -> ResearchTask:
        """The Phase 3 top-level entry point for a freshly created task
        (state == PLANNING): interpret -> (clarify if needed, else) route +
        gather + draft + validate + assess sufficiency. Stops at
        NEEDS_CLARIFICATION, INSUFFICIENT_EVIDENCE, or AWAITING_REVIEW -
        review and publication always require an explicit human action
        (submit_review, publish), never invoked automatically.
        """
        task = self.interpret_and_route(task_id)
        if task.state == TaskState.GATHERING_EVIDENCE:
            return self.run_autonomous_evidence_and_synthesis(task_id)
        return task

    # -- lifecycle: review and publication -----------------------------------

    def _security_context_summary(self, task_id: str) -> str:
        events = self.repository.list_security_events_for_task(task_id)
        if not events:
            return "no security events recorded for this task"
        actions = [e.action_taken.value for e in events]
        return f"{len(events)} security event(s) recorded: {actions}"

    def submit_review(
        self, task_id: str, reviewer_id: str, decision: ReviewDecision, comments: str = ""
    ) -> ResearchTask:
        """Phase 5: separation of duties and reviewer authorization are now
        checked for every decision (APPROVE, REJECT, REQUEST_REVISION), not
        only APPROVE as in Phase 1-4 - a deliberate strengthening recorded
        in docs/ROADMAP.md's Phase 5 deviation entry, since "reviewer" is a
        role distinct from "author" regardless of what the reviewer decides.
        The Review is stamped with the task's current claim_set_version
        (which version was actually reviewed) and a security_context
        summary, for the publication gate and audit trace respectively.
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.AWAITING_REVIEW:
            raise IllegalTransitionError(
                f"cannot review while task is in {task.state.value}"
            )

        policy = self.repository.policy_for_task(task_id)
        if policy.review_policy.separation_of_duties_required and reviewer_id == task.created_by:
            raise SelfApprovalError(
                f"reviewer_id ({reviewer_id}) must not equal created_by "
                f"({task.created_by}) for any review decision - "
                "docs/CLAIM_EVIDENCE_MODEL.md#reviewer-separation"
            )
        if reviewer_id not in AUTHORIZED_REVIEWER_IDS:
            raise UnauthorizedReviewerError(
                f"reviewer_id ({reviewer_id}) is not an authorized reviewer identity "
                "(afra.identity.AUTHORIZED_REVIEWER_IDS)"
            )

        # Effective claims only - a review evaluates the current claim set,
        # not superseded/replaced/withdrawn history (afra.domain
        # .claim_lifecycle.compute_effective_claims). Unchanged behaviour
        # for any task that has never used a revision-semantics action
        # (every claim is still ACTIVE).
        claims = compute_effective_claims(self.repository.list_claims_for_task(task_id))
        review = Review(
            task_id=task_id,
            reviewer_id=reviewer_id,
            decision=decision,
            comments=comments,
            claims_reviewed=[c.claim_id for c in claims],
            claim_set_version=task.claim_set_version,
            security_context=self._security_context_summary(task_id),
            policy_id=policy.policy_id,
            policy_version=policy.version,
        )
        self.repository.save_review(review)

        next_state = {
            ReviewDecision.APPROVE: TaskState.APPROVED,
            ReviewDecision.REQUEST_REVISION: TaskState.REVISION_REQUESTED,
            ReviewDecision.REJECT: TaskState.REJECTED,
        }[decision]
        return self._transition(task, next_state)

    def resume_from_revision(self, task_id: str, *, needs_new_evidence: bool = False) -> ResearchTask:
        """REVISION_REQUESTED -> GATHERING_EVIDENCE (if needs_new_evidence)
        or SYNTHESISING (otherwise) - docs/STATE_MACHINE.md's
        REVISION_REQUESTED transition row: "Reviewer flagged missing/wrong
        evidence" resumes at GATHERING_EVIDENCE, "Reviewer flagged
        drafting/wording issues only, evidence was adequate" resumes at
        SYNTHESISING. The SAME task_id continues; existing evidence and
        prior draft claims are retained either way (nothing is deleted) -
        see docs/STATE_MACHINE.md#resumability. This is the real entry
        point back into the revision loop; callers should not reach into
        Orchestrator._transition() directly.
        """
        task = self.repository.get_task(task_id)
        if task.state != TaskState.REVISION_REQUESTED:
            raise IllegalTransitionError(
                f"cannot resume a revision while task is in {task.state.value}"
            )
        target = TaskState.GATHERING_EVIDENCE if needs_new_evidence else TaskState.SYNTHESISING
        return self._transition(task, target)

    # -- claim revision semantics --------------------------------------------
    #
    # See docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics. Four
    # primitives on top of drafting (draft_claim/draft_claim_from_evidence
    # above), all operating on already-persisted claims:
    #
    # - replace_claim: a direct, targeted substitution (e.g. a reviewer-
    #   flagged factual correction) - drafts a brand new Claim itself and
    #   bumps claim_set_version immediately, since it's a standalone action,
    #   not part of a synthesis pass.
    # - supersede_claim: links an *already-drafted* new claim (e.g. one just
    #   produced by draft_claim_from_evidence during a revision's
    #   re-synthesis) as the successor of an old one. Does not bump
    #   claim_set_version itself - complete_synthesis() performs the one
    #   version bump for the whole re-synthesis pass, exactly as it always
    #   has.
    # - withdraw_claim: removes a claim from the effective set with no
    #   successor. Standalone, bumps claim_set_version immediately like
    #   replace_claim.
    # - object_to_claim: a reviewer's recorded concern about a claim - pure
    #   annotation, never changes lifecycle_status or claim_set_version by
    #   itself.
    #
    # None of these ever deletes a row or overwrites claim_text in place -
    # every historical claim stays queryable via
    # Repository.list_claims_for_task(); afra.domain.claim_lifecycle
    # .compute_effective_claims() is what narrows that down to "currently in
    # force." All four require the owning task not be in a terminal state
    # (afra.domain.enums.TERMINAL_STATES) - a published/blocked/rejected/
    # failed task's claim set is no longer revisable.

    def _require_revisable_task(self, task_id: str) -> ResearchTask:
        task = self.repository.get_task(task_id)
        if task.state in TERMINAL_STATES:
            raise IllegalClaimRevisionError(
                f"task {task_id} is in terminal state {task.state.value} - "
                "its claim set can no longer be revised"
            )
        return task

    def _get_active_claim_or_raise(self, task_id: str, claim_id: str) -> Claim:
        try:
            claim = self.repository.get_claim(claim_id)
        except KeyError:
            raise ClaimNotFoundError(claim_id) from None
        if claim.task_id != task_id:
            raise ClaimNotFoundError(f"claim {claim_id} does not belong to task {task_id}")
        if claim.lifecycle_status != ClaimLifecycleStatus.ACTIVE:
            raise IllegalClaimRevisionError(
                f"claim {claim_id} is {claim.lifecycle_status.value}, not ACTIVE - "
                "a claim that has already been replaced, superseded, or withdrawn "
                "cannot be revised again"
            )
        return claim

    def replace_claim(
        self,
        task_id: str,
        old_claim_id: str,
        claim_text: str,
        created_by: str,
        evidence_links: list[EvidenceLinkSpec] | None = None,
    ) -> Claim:
        """Directly substitutes old_claim_id with a newly-persisted claim -
        deterministic text substitution (no model call; this is a revision
        primitive, not a new drafting pathway), e.g. correcting a factual
        error a reviewer flagged without running a full re-synthesis pass.
        The new claim inherits old_claim_id's subquestion (if any), so
        afra.sufficiency.coverage's per-subquestion coverage check still
        finds it. Bumps task.claim_set_version immediately - the effective
        claim set has changed the moment this returns, which means any
        existing APPROVE review is now stale for the new version (see
        afra.review.publication_gate's version-integrity check) - no
        separate "invalidate the approval" step exists or is needed.

        Validates the new claim immediately (afra.validation.validator
        .validate_claim), unlike draft_claim/draft_claim_from_evidence,
        which leave validation to a later explicit run_validation() call
        while the task is still in VALIDATING. replace_claim is meant to be
        usable *after* a task has already left VALIDATING (during
        REVISION_REQUESTED, AWAITING_REVIEW, or even APPROVED) - there is
        no VALIDATING state to route back through, and an un-validated
        claim would otherwise never get a support_status at all, which the
        publication gate would then correctly (but unhelpfully) reject as
        "UNVALIDATED" forever.
        """
        task = self._require_revisable_task(task_id)
        old_claim = self._get_active_claim_or_raise(task_id, old_claim_id)

        new_version = task.claim_set_version + 1
        new_claim = Claim(
            task_id=task_id,
            claim_text=claim_text,
            model_id="human-revision",
            created_by=created_by,
            subquestion=old_claim.subquestion,
            claim_set_version=new_version,
            predecessor_claim_id=old_claim_id,
        )
        self.repository.save_claim(new_claim)
        for spec in evidence_links or []:
            self.repository.save_link(
                ClaimEvidenceLink(
                    claim_id=new_claim.claim_id,
                    evidence_id=spec.evidence_id,
                    quote_span=spec.quote_span,
                    support_contribution=spec.support_contribution,
                )
            )
        validation_result = validate_claim(new_claim.claim_id, self.repository)
        self.repository.record_validation_result(validation_result)

        old_claim.lifecycle_status = ClaimLifecycleStatus.REPLACED
        old_claim.successor_claim_id = new_claim.claim_id
        self.repository.save_claim(old_claim)

        task.claim_set_version = new_version
        self.repository.save_task(task)
        return self.repository.get_claim(new_claim.claim_id)

    def supersede_claim(self, task_id: str, old_claim_id: str, new_claim_id: str) -> None:
        """Links new_claim_id (an already-persisted claim - typically one
        just drafted via draft_claim_from_evidence/draft_claim during a
        revision's re-synthesis pass) as the successor of old_claim_id.
        Intended to be called for each subquestion a revision re-drafts,
        between drafting the new claims and calling complete_synthesis() -
        that single complete_synthesis() call is what bumps
        claim_set_version for the whole batch, exactly as it always has;
        this method does not bump it a second time per claim.
        """
        self._require_revisable_task(task_id)
        old_claim = self._get_active_claim_or_raise(task_id, old_claim_id)
        try:
            new_claim = self.repository.get_claim(new_claim_id)
        except KeyError:
            raise ClaimNotFoundError(new_claim_id) from None
        if new_claim.task_id != task_id:
            raise ClaimNotFoundError(f"claim {new_claim_id} does not belong to task {task_id}")

        old_claim.lifecycle_status = ClaimLifecycleStatus.SUPERSEDED
        old_claim.successor_claim_id = new_claim.claim_id
        self.repository.save_claim(old_claim)

        new_claim.predecessor_claim_id = old_claim_id
        self.repository.save_claim(new_claim)

    def withdraw_claim(self, task_id: str, claim_id: str, reason: str) -> ResearchTask:
        """Removes claim_id from the effective claim set with no
        successor - e.g. its evidence turned out irrelevant, or it should
        never have been drafted. Standalone action: bumps
        task.claim_set_version immediately, like replace_claim.
        """
        task = self._require_revisable_task(task_id)
        claim = self._get_active_claim_or_raise(task_id, claim_id)

        claim.lifecycle_status = ClaimLifecycleStatus.WITHDRAWN
        claim.withdrawn_reason = reason
        self.repository.save_claim(claim)

        task.claim_set_version += 1
        self.repository.save_task(task)
        return task

    def object_to_claim(self, task_id: str, claim_id: str, reviewer_id: str, reason: str) -> ClaimObjection:
        """Records a reviewer's objection to a claim - pure annotation:
        never changes the claim's lifecycle_status or bumps
        claim_set_version by itself (an objection alone does not change the
        effective claim set; addressing it via replace_claim/
        supersede_claim/withdraw_claim does). Enforces the same reviewer-
        separation and reviewer-authorization rules as submit_review(), since
        an objection is a reviewer action in exactly the same sense a review
        decision is. Unlike the other three revision primitives, this may
        target a claim of any lifecycle_status - objecting to something
        already superseded is still a legitimate historical annotation
        (e.g. explaining *why* it needed to be superseded).
        """
        task = self.repository.get_task(task_id)  # objections are allowed even on a terminal task - pure history
        if reviewer_id == task.created_by:
            raise SelfApprovalError(
                f"reviewer_id ({reviewer_id}) must not equal created_by "
                f"({task.created_by}) - an objection is a reviewer action"
            )
        if reviewer_id not in AUTHORIZED_REVIEWER_IDS:
            raise UnauthorizedReviewerError(
                f"reviewer_id ({reviewer_id}) is not an authorized reviewer identity "
                "(afra.identity.AUTHORIZED_REVIEWER_IDS)"
            )
        try:
            claim = self.repository.get_claim(claim_id)
        except KeyError:
            raise ClaimNotFoundError(claim_id) from None
        if claim.task_id != task_id:
            raise ClaimNotFoundError(f"claim {claim_id} does not belong to task {task_id}")

        objection = ClaimObjection(
            task_id=task_id,
            claim_id=claim_id,
            reviewer_id=reviewer_id,
            reason=reason,
            claim_set_version=task.claim_set_version,
        )
        self.repository.save_objection(objection)
        return objection

    def resolve_security_event(self, security_event_id: str, resolved_by: str) -> SecurityEvent:
        """Phase 5's real resolution workflow for a require_approval
        SecurityEvent, per docs/CLAIM_EVIDENCE_MODEL.md#securityevent's
        already-documented resolved_by field. Deliberately does NOT touch
        ResearchTask.state: the task that produced this event is already
        SECURITY_BLOCKED (terminal, no outgoing transitions - see
        docs/STATE_MACHINE.md's invariants), and no override path for that
        exists anywhere in this project's documented design - resolving the
        event records governance sign-off for the audit trail, it does not
        revive the task. Only a `block` (not require_approval) action
        cannot be resolved at all: a hard block has no human-override
        concept in this project - see docs/DATA_CLASSIFICATION.md.
        """
        if resolved_by not in GOVERNANCE_REVIEWER_IDS:
            raise UnauthorizedReviewerError(
                f"resolved_by ({resolved_by}) is not an authorized governance identity "
                "(afra.identity.GOVERNANCE_REVIEWER_IDS)"
            )
        event = self.repository.get_security_event(security_event_id)
        if event.action_taken != DLPAction.REQUIRE_APPROVAL:
            raise SecurityPolicyError(
                f"SecurityEvent {security_event_id} has action_taken="
                f"{event.action_taken.value}; only require_approval events can be "
                "resolved (docs/DATA_CLASSIFICATION.md - a block has no override path)"
            )
        self.repository.resolve_security_event(security_event_id, resolved_by)
        return self.repository.get_security_event(security_event_id)

    def publish(self, task_id: str) -> ResearchTask:
        """APPROVED -> PUBLISHED, gated by
        afra.review.publication_gate.evaluate_publication_gate re-checked
        against current persisted state at the moment of publication - see
        docs/STATE_MACHINE.md#invariants. Raises PublicationGateError
        (task remains APPROVED, untouched) if the gate fails; never
        publishes partially or "provisionally".
        """
        task = self.repository.get_task(task_id)
        result = evaluate_publication_gate(task_id, self.repository)
        self.repository.record_publication_decision(task, result)
        if not result.passed:
            from afra.domain.errors import PublicationGateError

            raise PublicationGateError("; ".join(result.reasons))
        return self._transition(task, TaskState.PUBLISHED)
