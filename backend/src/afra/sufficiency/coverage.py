"""compute_sufficiency() - the Phase 3 evidence-coverage/sufficiency check.

Returns a plain dict (not a new persisted entity/table - see
docs/PHASE_3_REPORT.md): the orchestrator stores the result directly on
ResearchTask.resolved_scope["sufficiency_result"], the same
JSON-on-the-task pattern used for research_scope and subquestions.

This subsumes and extends Phase 1/2's per-claim support_status check
(afra.orchestrator.orchestrator.evaluate_sufficiency_and_advance) with the
richer, subquestion/comparison-aware picture Phase 3 asks for: not just "is
every claim SUPPORTED" but "does every subquestion have evidence, are both
comparison sides represented, are there unresolved contradictions."

Important: the per-claim status checks (unsupported / conflicting /
insufficient) run over *every* claim on the task, not only claims that
have a `subquestion` recorded - Phase 1/2 tasks never set `subquestion` at
all, and this function must still catch a bad claim on those tasks. Only
the subquestion-coverage check is limited to claims that do have one.
"""

from __future__ import annotations

from afra.domain.claim_lifecycle import compute_effective_claims
from afra.domain.enums import SupportStatus
from afra.domain.models import ResearchTask
from afra.storage.repository import Repository
from afra.tools.fixtures import search_fixture_documents


def compute_sufficiency(task: ResearchTask, repository: Repository) -> dict:
    """Scoped to the task's currently *effective* claims
    (afra.domain.claim_lifecycle.compute_effective_claims) - a claim
    already replaced/superseded/withdrawn by a revision-semantics action is
    history, not something sufficiency should be computed against. Before
    any such action has ever been used on a task, every claim is still
    ACTIVE, so this is identical to considering every claim - unchanged
    behaviour for every pre-existing call path.
    """
    policy = repository.policy_for_task(task.task_id)
    evidence_policy = policy.evidence_policy
    scope_dict = task.resolved_scope.get("research_scope", {})
    companies = (scope_dict.get("companies") or {}).get("value") or []
    subquestions: list[str] = task.resolved_scope.get("subquestions", [])
    claims = compute_effective_claims(repository.list_claims_for_task(task.task_id))
    claims_by_subquestion = {c.subquestion: c for c in claims if c.subquestion}

    # 1. Per-subquestion coverage - only meaningful for claims that record
    #    which subquestion they answer (Phase 3's autonomous drafting path).
    subquestions_covered: dict[str, bool] = {}
    for subquestion in subquestions:
        claim = claims_by_subquestion.get(subquestion)
        if claim is None:
            subquestions_covered[subquestion] = False
        else:
            links = repository.list_links_for_claim(claim.claim_id)
            subquestions_covered[subquestion] = len({link.evidence_id for link in links}) >= evidence_policy.minimum_evidence_per_subquestion

    # 2. Per-claim status checks - run over every claim on the task,
    #    regardless of whether it's tied to a subquestion (Phase 1/2 claims
    #    never are, and must still be caught here).
    claims_without_evidence: list[str] = []
    unresolved_contradictions: list[str] = []
    unsupported_claims: list[str] = []
    insufficient_evidence_claims: list[str] = []
    for claim in claims:
        links = repository.list_links_for_claim(claim.claim_id)
        if not links:
            claims_without_evidence.append(claim.claim_id)
        if claim.support_status == SupportStatus.CONFLICTING_EVIDENCE:
            unresolved_contradictions.append(claim.claim_id)
        elif claim.support_status == SupportStatus.UNSUPPORTED:
            unsupported_claims.append(claim.claim_id)
        elif claim.support_status == SupportStatus.INSUFFICIENT_EVIDENCE:
            insufficient_evidence_claims.append(claim.claim_id)

    # 3. Comparison-side coverage - only meaningful when the resolved scope
    #    names 2+ companies (a comparison task).
    missing_sides: list[str] = []
    comparison_sides_represented: bool | None = None
    if len(companies) >= 2:
        for company in companies:
            company_doc_ids = {d.document_id for d in search_fixture_documents(company=company)}
            has_evidence = any(
                repository.get_evidence(link.evidence_id).source_document_id in company_doc_ids
                for claim in claims
                for link in repository.list_links_for_claim(claim.claim_id)
            )
            if not has_evidence:
                missing_sides.append(company)
        comparison_sides_represented = not missing_sides

    uncovered_subquestions = [q for q, covered in subquestions_covered.items() if not covered]

    reasons = []
    if uncovered_subquestions:
        reasons.append(f"{len(uncovered_subquestions)} subquestion(s) do not meet the minimum evidence requirement")
    if missing_sides:
        reasons.append(f"no evidence found for: {', '.join(missing_sides)}")
    if unresolved_contradictions and evidence_policy.conflicting_evidence_blocks_sufficiency:
        reasons.append(f"{len(unresolved_contradictions)} claim(s) have unresolved conflicting evidence")
    if unsupported_claims:
        reasons.append(f"{len(unsupported_claims)} claim(s) are unsupported by their cited evidence")
    if insufficient_evidence_claims and evidence_policy.insufficient_evidence_requires_abstention:
        reasons.append(f"{len(insufficient_evidence_claims)} claim(s) have insufficient evidence")

    is_sufficient = not reasons

    return {
        "policy_id": policy.policy_id,
        "policy_version": policy.version,        "subquestions_covered": subquestions_covered,
        "claims_without_evidence": claims_without_evidence,
        "unresolved_contradictions": unresolved_contradictions,
        "unsupported_claims": unsupported_claims,
        "insufficient_evidence_claims": insufficient_evidence_claims,
        "comparison_sides_represented": comparison_sides_represented,
        "missing_sides": missing_sides,
        "is_sufficient": is_sufficient,
        "reason": "; ".join(reasons) if reasons else "all subquestions covered with supported claims",
    }
