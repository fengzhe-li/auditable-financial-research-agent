"""Failure-injection tests - docs/ROADMAP.md's "Failure injection cases"
list, built out in Phase 6 as promised there. Each test proves the system
produces a safe, legible failure (a specific state, a specific
SecurityEvent, or a specific error) for a deliberately broken/adversarial
input - never a fabricated success.

Kept separate from the benchmark's scored tasks (afra/benchmark/) and from
core correctness tests (test_state_machine.py, test_validator.py, ...):
this file exists specifically to enumerate the 9 named failure-injection
cases docs/ROADMAP.md commits to, each with a direct, deterministic
pass/fail assertion, per docs/ROADMAP.md's Phase 6 instruction to add
benchmark/evaluation tests separately from core correctness tests.

Several cases (conflicting evidence, prompt injection, restricted-data
external-model attempt) already have thorough direct coverage elsewhere
(test_phase2_end_to_end.py, test_phase4_end_to_end.py,
test_phase5_review_workflow.py) - this file adds one focused test per case
under its own name, for a single place that maps every failure-injection
case docs/ROADMAP.md lists to an actual test, rather than leaving that
mapping implicit.
"""

from __future__ import annotations

import afra.orchestrator.orchestrator as orchestrator_module
import pytest

from afra.domain.enums import SupportContribution, SupportStatus, TaskState
from afra.domain.errors import SecurityPolicyError
from afra.domain.models import Evidence
from afra.orchestrator.orchestrator import EvidenceLinkSpec
from afra.tools.fixtures import UnknownDocumentError
from afra.tools.search_documents import search_documents


# -- 1. missing filing ---------------------------------------------------------


def test_missing_filing_fails_the_task_safely(orchestrator):
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    with pytest.raises(UnknownDocumentError):
        orchestrator.gather_evidence_from_document(task.task_id, "NORTHWIND-2025-10K", "risk_factors")
    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert "retrieve_section failed" in reloaded.resolved_scope["failure_reason"]


# -- 2. stale document ----------------------------------------------------------


def test_stale_document_retrieval_is_honestly_timestamped_not_silently_treated_as_current(orchestrator):
    """Explicitly retrieving the 2024 (stale) filing instead of the 2025
    (current) one succeeds - this system does not itself detect staleness
    (a real, disclosed limitation, see docs/PHASE_6_REPORT.md) - but the
    resulting Evidence.source_timestamp honestly records the STALE
    document's real date, never silently presented as current. This is
    what "safe, legible failure" means for staleness specifically: the
    system doesn't crash, and it doesn't lie about the evidence's age
    either.
    """
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2024-10K", "risk_factors")
    assert evidence.source_timestamp.year == 2024
    assert "do not currently expect this to require a material increase" in evidence.raw_text


# -- 3. irrelevant retrieval ------------------------------------------------------


def test_irrelevant_retrieval_produces_unsupported_not_a_fabricated_match(orchestrator):
    """Evidence is genuinely retrieved (not missing), but the claim cites
    text that isn't actually in it - modelling "the router grabbed evidence
    that looked relevant but doesn't actually support this specific claim".
    The validator must not paper over the mismatch.
    """
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "FABRIKAM-2024-10K", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)
    orchestrator.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp expects AI infrastructure capex to increase materially.",
        evidence_links=[
            EvidenceLinkSpec(evidence.evidence_id, "expect this capital expenditure to increase materially", SupportContribution.SUPPORTS)
        ],
        created_by="analyst_1",
    )
    orchestrator.complete_synthesis(task.task_id)
    results = orchestrator.run_validation(task.task_id)
    assert results[0].computed_support_status == SupportStatus.UNSUPPORTED


# -- 4. conflicting evidence ------------------------------------------------------


def test_conflicting_evidence_does_not_reach_published(orchestrator):
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    evidence_2024 = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2024-10K", "risk_factors")
    evidence_2025 = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)
    orchestrator.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp does not expect material AI infrastructure capex growth.",
        evidence_links=[
            EvidenceLinkSpec(evidence_2024.evidence_id, "do not currently expect this to require a material increase", SupportContribution.SUPPORTS),
            EvidenceLinkSpec(evidence_2025.evidence_id, "expect this capital expenditure to increase materially", SupportContribution.CONTRADICTS),
        ],
        created_by="analyst_1",
    )
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    task_after = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    assert task_after.state == TaskState.INSUFFICIENT_EVIDENCE


# -- 5. malformed tool output ------------------------------------------------------


def test_malformed_tool_output_is_caught_by_validation_not_treated_as_supported(orchestrator):
    """Simulates a tool returning garbage content (e.g. an empty or
    corrupted section) rather than raising - the failure mode isn't an
    exception, it's silently-wrong data. The validator's citation check
    (a real substring search, not an LLM's opinion) must still correctly
    refuse to call this SUPPORTED.
    """
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    malformed_evidence = Evidence(
        task_id=task.task_id,
        source_document_id="CONTOSO-2025-10K",
        source_location="CONTOSO-2025-10K#risk_factors",
        raw_text="",  # simulated malformed/corrupted tool output
        retrieval_tool="retrieve_section",
        content_hash="deadbeef",
    )
    orchestrator.repository.save_evidence(malformed_evidence)
    orchestrator.complete_evidence_gathering(task.task_id)
    orchestrator.draft_claim(
        task.task_id,
        prompt="Contoso Cloud Corp expects AI infrastructure capex to increase materially.",
        evidence_links=[
            EvidenceLinkSpec(malformed_evidence.evidence_id, "increase materially", SupportContribution.SUPPORTS)
        ],
        created_by="analyst_1",
    )
    orchestrator.complete_synthesis(task.task_id)
    results = orchestrator.run_validation(task.task_id)
    assert results[0].computed_support_status == SupportStatus.UNSUPPORTED


# -- 6. empty retrieval -------------------------------------------------------------


def test_empty_retrieval_reaches_insufficient_evidence(orchestrator):
    assert search_documents(company="Northwind Traders") == []
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    orchestrator.complete_evidence_gathering(task.task_id)  # zero evidence allowed through
    orchestrator.draft_claim_without_evidence(task.task_id, "What does Northwind Traders disclose?", created_by="analyst_1")
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    task_after = orchestrator.evaluate_sufficiency_and_advance(task.task_id)
    assert task_after.state == TaskState.INSUFFICIENT_EVIDENCE


# -- 7. tool failure / timeout simulation --------------------------------------------


def test_tool_timeout_fails_the_task_instead_of_leaving_it_stuck(orchestrator, monkeypatch):
    """A tool-layer exception that isn't UnknownDocumentError/KeyError (a
    simulated timeout, standing in for a real network/IO failure this
    fixture-only system has no real equivalent of) must still produce a
    safe, legible FAILED transition - docs/STATE_MACHINE.md has always
    documented GATHERING_EVIDENCE -> FAILED for exactly this case ("Tool
    layer exhausts retries on a required tool call"). Phase 6 failure
    injection found this wasn't actually implemented (only the two known
    document/section error shapes were caught; anything else propagated
    uncaught and left the task stuck in GATHERING_EVIDENCE forever) and
    fixed afra.orchestrator.orchestrator.gather_evidence_from_document() to
    match the documented behaviour - see docs/ROADMAP.md's Phase 6
    deviation entry.
    """

    def _simulated_timeout(*args, **kwargs):
        raise TimeoutError("simulated tool timeout")

    monkeypatch.setattr(orchestrator_module, "retrieve_section", _simulated_timeout)

    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    with pytest.raises(TimeoutError):
        orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-2025-10K", "risk_factors")

    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.FAILED
    assert "retrieve_section failed" in reloaded.resolved_scope["failure_reason"]

    tool_calls = orchestrator.repository.list_tool_calls_for_task(task.task_id)
    assert tool_calls[-1].succeeded is False


# -- 8. prompt injection -------------------------------------------------------------


def test_prompt_injection_cannot_trigger_an_unauthorised_tool_call(orchestrator):
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "FABRIKAM-2025-10K-ANNOTATED", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)
    orchestrator.draft_claim_from_evidence(task.task_id, "subquestion", [evidence], created_by="analyst_1")
    orchestrator.complete_synthesis(task.task_id)
    orchestrator.run_validation(task.task_id)
    tool_calls = orchestrator.repository.list_tool_calls_for_task(task.task_id)
    assert {tc.tool_name for tc in tool_calls} <= {"search_documents", "get_document", "retrieve_section", "route_subquestion"}


# -- 9. restricted-data external-model attempt ---------------------------------------


def test_restricted_data_external_model_attempt_is_blocked(orchestrator, provider):
    task = orchestrator.create_task(question_text="q", created_by="analyst_1")
    orchestrator.mark_plan_complete(task.task_id)
    evidence = orchestrator.gather_evidence_from_document(task.task_id, "CONTOSO-MNPI-NOTE", "risk_factors")
    orchestrator.complete_evidence_gathering(task.task_id)
    with pytest.raises(SecurityPolicyError):
        orchestrator.draft_claim(
            task.task_id,
            prompt="Summarise the restricted note.",
            evidence_links=[
                EvidenceLinkSpec(evidence.evidence_id, "material non-public information", SupportContribution.SUPPORTS)
            ],
            created_by="analyst_1",
        )
    assert provider.call_count == 0
    reloaded = orchestrator.repository.get_task(task.task_id)
    assert reloaded.state == TaskState.SECURITY_BLOCKED
