"""Policy persistence and enforcement contracts; all providers are test doubles."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from afra.api.app import app, get_repository
from afra.domain.enums import Classification as C, ProviderClass as P, PolicyStatus, TaskState, ReviewDecision, SupportStatus, DLPAction, SupportContribution
from afra.domain.errors import InvalidPolicyError, PolicyVersionConflictError, PolicyNotFoundError, SecurityPolicyError, SelfApprovalError, PublicationGateError
from afra.domain.models import ResearchTask, Claim, Review, ValidationResult, Evidence, ClaimEvidenceLink
from afra.governance.default_policy import DEFAULT_GOVERNANCE_POLICY as DEFAULT
from afra.governance.policy import GovernancePolicy, validate_policy_or_raise
from afra.policy.routing_policy import ROUTING_POLICY
from afra.policy.enforcement import enforce_model_call_policy
from afra.review.publication_gate import evaluate_publication_gate
from afra.storage.repository import Repository, SCHEMA
from afra.sufficiency.coverage import compute_sufficiency
from afra.trace import assemble_trace


def changed_policy(**kwargs):
    return replace(DEFAULT, version=2, effective_from=None, **kwargs)


def routing_policy(**kwargs):
    rules = dict(DEFAULT.data_class_policies)
    rules[C.PUBLIC] = replace(rules[C.PUBLIC], **kwargs)
    return changed_policy(data_class_policies=rules)


def install(repository, policy):
    repository.save_policy(policy)
    repository.activate_policy(*policy.identity())


def make_claim(repository, task, status=SupportStatus.SUPPORTED, subquestion=None):
    claim = Claim(task_id=task.task_id, claim_text="claim", model_id="test", created_by=task.created_by, subquestion=subquestion)
    repository.save_claim(claim)
    repository.record_validation_result(ValidationResult(claim_id=claim.claim_id, computed_support_status=status))
    return repository.get_claim(claim.claim_id)


def test_default_roundtrip_and_legacy_rules():
    assert GovernancePolicy.from_dict(DEFAULT.to_dict()) == DEFAULT
    validate_policy_or_raise(DEFAULT)
    for classification, rule in ROUTING_POLICY.items():
        actual = DEFAULT.data_class_policy(classification)
        assert actual.allowed_provider_classes == rule.allowed_provider_classes
        assert actual.require_human_approval == rule.requires_approval
    assert ResearchTask("q", "a").policy_id == DEFAULT.policy_id
    assert ResearchTask("q", "a").policy_version == DEFAULT.version


@pytest.mark.parametrize("field", ["policy_id", "version", "name", "status", "description", "created_at", "data_class_policies", "review_policy", "evidence_policy"])
def test_missing_metadata_rejected(field):
    spec = DEFAULT.to_dict()
    del spec[field]
    with pytest.raises(InvalidPolicyError):
        GovernancePolicy.from_dict(spec)


@pytest.mark.parametrize("field,value", [("version", True), ("version", 0), ("version", "2"), ("policy_id", " "), ("name", None), ("status", "unknown"), ("created_at", None), ("created_at", "invalid"), ("created_at", "2026-01-01T00:00:00"), ("effective_from", 2)])
def test_invalid_metadata_rejected(field, value):
    spec = DEFAULT.to_dict()
    spec[field] = value
    with pytest.raises(InvalidPolicyError):
        GovernancePolicy.from_dict(spec)


@pytest.mark.parametrize("mutation", [
    lambda d: d["data_class_policies"].update(SECRET={}),
    lambda d: d["data_class_policies"].pop("PUBLIC"),
    lambda d: d["data_class_policies"]["PUBLIC"].update(classification="INTERNAL"),
    lambda d: d["data_class_policies"]["PUBLIC"].update(allowed_provider_classes=["UNKNOWN"]),
    lambda d: d["data_class_policies"]["PUBLIC"].update(blocked_provider_classes=["EXTERNAL_STANDARD"]),
    lambda d: d["data_class_policies"]["PUBLIC"].update(require_private_provider=True, allowed_provider_classes=["EXTERNAL_STANDARD"]),
    lambda d: d["data_class_policies"]["PUBLIC"].update(unconditional_block=True),
    lambda d: d["data_class_policies"]["PUBLIC"].update(require_human_approval="false"),
    lambda d: d["review_policy"].update(human_review_required=False),
    lambda d: d["review_policy"].update(publication_requires_current_version_approval=False),
    lambda d: d["review_policy"].update(conflict_requires_abstention=False),
    lambda d: d["evidence_policy"].update(minimum_evidence_per_subquestion=0),
    lambda d: d["evidence_policy"].update(minimum_evidence_per_subquestion=1.2),
    lambda d: d["evidence_policy"].update(minimum_evidence_per_subquestion=True),
    lambda d: d["evidence_policy"].update(unsupported_claims_block_publication=False),
    lambda d: d["evidence_policy"].update(insufficient_evidence_requires_abstention=False),
    lambda d: d["review_policy"].update(human_review_requiredd=True),
    lambda d: d.update(data_class_policies=[]),
    lambda d: d.update(review_policy=None),
])
def test_invalid_rules_rejected(mutation):
    spec = DEFAULT.to_dict()
    mutation(spec)
    with pytest.raises(InvalidPolicyError):
        GovernancePolicy.from_dict(spec)


def test_direct_unknown_enums_rejected():
    rules = dict(DEFAULT.data_class_policies)
    rules["UNKNOWN"] = rules[C.PUBLIC]
    with pytest.raises(InvalidPolicyError):
        validate_policy_or_raise(changed_policy(data_class_policies=rules))
    rules = dict(DEFAULT.data_class_policies)
    rules[C.PUBLIC] = replace(rules[C.PUBLIC], allowed_provider_classes={"UNKNOWN"})
    with pytest.raises(InvalidPolicyError):
        validate_policy_or_raise(changed_policy(data_class_policies=rules))


def test_deep_immutability_and_defensive_copy():
    rules = dict(DEFAULT.data_class_policies)
    policy = changed_policy(data_class_policies=rules)
    rules.clear()
    assert len(policy.data_class_policies) == 4
    with pytest.raises(TypeError):
        policy.data_class_policies[C.PUBLIC] = DEFAULT.data_class_policy(C.INTERNAL)
    with pytest.raises(FrozenInstanceError):
        policy.version = 8
    with pytest.raises(FrozenInstanceError):
        policy.review_policy.human_review_required = False
    with pytest.raises(AttributeError):
        policy.data_class_policy(C.PUBLIC).allowed_provider_classes.add(P.PRIVATE_LOCAL)
    exported = policy.to_dict()
    exported["data_class_policies"].clear()
    assert len(policy.data_class_policies) == 4


def test_versions_duplicate_identity_and_durable_activation(repository, db_path, orchestrator):
    old = orchestrator.create_task("old", "analyst_1")
    before = repository.get_policy("default", 1).to_dict()
    install(repository, changed_policy(name="Second version"))
    new = orchestrator.create_task("new", "analyst_1")
    assert (old.policy_version, new.policy_version) == (1, 2)
    for policy in (DEFAULT, changed_policy(name="Different contents")):
        with pytest.raises(PolicyVersionConflictError):
            repository.save_policy(policy)
    reopened = Repository(db_path)
    assert reopened.get_active_policy().version == 2
    assert reopened.policy_for_task(old.task_id).to_dict() == before
    assert reopened.policy_for_task(new.task_id).version == 2
    assert {t.policy_version for t in reopened.list_tasks()} == {1, 2}
    reopened.close()
    old.policy_version = 2
    with pytest.raises(PolicyVersionConflictError):
        repository.save_task(old)
    with pytest.raises(sqlite3.IntegrityError):
        repository._conn.execute("UPDATE governance_policies SET specification = '{}' WHERE version = 1")
    with pytest.raises(sqlite3.IntegrityError):
        repository._conn.execute("DELETE FROM governance_policies WHERE version = 1")


@pytest.mark.parametrize("status", [PolicyStatus.DRAFT, PolicyStatus.SUPERSEDED])
def test_inactive_spec_cannot_activate(repository, status):
    repository.save_policy(changed_policy(status=status))
    with pytest.raises(InvalidPolicyError):
        repository.activate_policy("default", 2)
    assert repository.get_active_policy().version == 1


def test_future_policy_and_unknown_binding_rejected(repository):
    repository.save_policy(replace(DEFAULT, version=2, effective_from=datetime.now(timezone.utc)+timedelta(days=1)))
    with pytest.raises(InvalidPolicyError):
        repository.activate_policy("default", 2)
    with pytest.raises(PolicyNotFoundError):
        repository.save_task(ResearchTask("q", "a", policy_version=99))


@pytest.mark.parametrize("classification,expected", [(C.PUBLIC, P.EXTERNAL_STANDARD), (C.INTERNAL, P.ENTERPRISE_APPROVED), (C.CONFIDENTIAL, P.PRIVATE_LOCAL), (C.RESTRICTED, None)])
def test_default_routing_all_classes(classification, expected, providers):
    result = enforce_model_call_policy(classification=classification, prompt="safe", requested_provider_class=P.EXTERNAL_STANDARD, providers=providers)
    assert result.selected_provider_class == expected
    assert result.allowed == (expected is not None)
    assert (result.policy_id, result.policy_version) == DEFAULT.identity()


def test_assigned_routing_survives_activation(orchestrator, repository, provider, private_provider):
    old = orchestrator.create_task("old", "analyst_1")
    install(repository, routing_policy(allowed_provider_classes={P.PRIVATE_LOCAL}, require_private_provider=True, blocked_provider_classes={P.EXTERNAL_STANDARD}))
    new = orchestrator.create_task("new", "analyst_1")
    for task in (old, new):
        task.state = TaskState.SYNTHESISING
        repository.save_task(task)
        orchestrator.draft_claim(task.task_id, prompt="safe", evidence_links=[], created_by="analyst_1")
    assert provider.call_count == private_provider.call_count == 1
    for task, version, selected in ((old, 1, provider), (new, 2, private_provider)):
        event = repository.list_security_events_for_task(task.task_id)[0]
        assert event.policy_version == version
        assert event.selected_provider_id == selected.provider_id
        assert assemble_trace(task.task_id, repository)["security_events"][0]["policy_version"] == version


def test_required_model_approval_blocks_before_provider(orchestrator, repository, providers):
    install(repository, routing_policy(require_human_approval=True))
    task = orchestrator.create_task("q", "a")
    task.state = TaskState.SYNTHESISING
    repository.save_task(task)
    with pytest.raises(SecurityPolicyError):
        orchestrator.draft_claim(task.task_id, prompt="safe", evidence_links=[], created_by="a")
    assert sum(p.call_count for p in providers.values()) == 0
    event = repository.list_security_events_for_task(task.task_id)[0]
    assert event.action_taken == DLPAction.REQUIRE_APPROVAL
    assert event.policy_version == 2
    assert repository.get_task(task.task_id).state == TaskState.SECURITY_BLOCKED


def test_gate_and_review_use_bound_policy_and_trace(orchestrator, repository):
    old = orchestrator.create_task("old", "reviewer_1")
    install(repository, changed_policy(review_policy=replace(DEFAULT.review_policy, separation_of_duties_required=False)))
    new = orchestrator.create_task("new", "reviewer_1")
    for task in (old, new):
        task.state = TaskState.AWAITING_REVIEW
        repository.save_task(task)
        make_claim(repository, task)
    with pytest.raises(SelfApprovalError):
        orchestrator.submit_review(old.task_id, "reviewer_1", ReviewDecision.APPROVE)
    orchestrator.submit_review(new.task_id, "reviewer_1", ReviewDecision.APPROVE)
    assert evaluate_publication_gate(new.task_id, repository).passed
    orchestrator.publish(new.task_id)
    trace = assemble_trace(new.task_id, repository)
    assert trace["policy_version"] == trace["reviews"][0]["policy_version"] == 2
    assert trace["publication_decisions"][0]["passed"] is True
    assert trace["publication_decisions"][0]["policy_version"] == 2
    # A persisted self-approval still fails for the older task.
    old.state = TaskState.APPROVED
    repository.save_task(old)
    repository.save_review(Review(task_id=old.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE))
    with pytest.raises(PublicationGateError):
        orchestrator.publish(old.task_id)
    assert repository.list_publication_decisions(old.task_id)[0]["policy_version"] == 1
    assert not repository.list_publication_decisions(old.task_id)[0]["passed"]


def test_gate_requires_human_current_version_and_same_policy(repository):
    install(repository, changed_policy())
    task = ResearchTask("q", "analyst_1", state=TaskState.APPROVED, policy_version=2, claim_set_version=2)
    repository.save_task(task)
    make_claim(repository, task)
    assert any("no APPROVE" in r for r in evaluate_publication_gate(task.task_id, repository).reasons)
    review = Review(task_id=task.task_id, reviewer_id="reviewer_1", decision=ReviewDecision.APPROVE, claim_set_version=1, policy_version=2)
    repository.save_review(review)
    assert any("stale approval" in r for r in evaluate_publication_gate(task.task_id, repository).reasons)
    review.claim_set_version = 2
    review.policy_version = 1
    repository.save_review(review)
    assert any("different governance policy" in r for r in evaluate_publication_gate(task.task_id, repository).reasons)
    review.policy_version = 2
    repository.save_review(review)
    assert evaluate_publication_gate(task.task_id, repository).passed


def test_conflict_can_route_to_review_but_never_publish(repository, orchestrator):
    old = orchestrator.create_task("old", "analyst_1")
    install(repository, changed_policy(review_policy=replace(DEFAULT.review_policy, conflict_requires_abstention=False), evidence_policy=replace(DEFAULT.evidence_policy, conflicting_evidence_blocks_sufficiency=False)))
    new = orchestrator.create_task("new", "analyst_1")
    for task in (old, new):
        task.state = TaskState.VALIDATING
        repository.save_task(task)
        make_claim(repository, task, SupportStatus.CONFLICTING_EVIDENCE)
    assert orchestrator.evaluate_sufficiency_and_advance(old.task_id).state == TaskState.INSUFFICIENT_EVIDENCE
    assert orchestrator.evaluate_sufficiency_and_advance(new.task_id).state == TaskState.AWAITING_REVIEW
    orchestrator.submit_review(new.task_id, "reviewer_1", ReviewDecision.APPROVE)
    with pytest.raises(PublicationGateError):
        orchestrator.publish(new.task_id)


def test_minimum_evidence_is_policy_driven(repository, orchestrator):
    old = orchestrator.create_task("old", "analyst_1")
    install(repository, changed_policy(evidence_policy=replace(DEFAULT.evidence_policy, minimum_evidence_per_subquestion=2)))
    new = orchestrator.create_task("new", "analyst_1")
    for task in (old, new):
        task.resolved_scope["subquestions"] = ["q"]
        repository.save_task(task)
        claim = make_claim(repository, task, subquestion="q")
        evidence = Evidence(task_id=task.task_id, source_document_id="doc", source_location="p1", raw_text="x", retrieval_tool="test", content_hash="x")
        repository.save_evidence(evidence)
        repository.save_link(ClaimEvidenceLink(claim_id=claim.claim_id, evidence_id=evidence.evidence_id, quote_span="x", support_contribution=SupportContribution.SUPPORTS))
        result = compute_sufficiency(task, repository)
        assert result["is_sufficient"] == (task.policy_version == 1)
        assert result["policy_version"] == task.policy_version


def test_legacy_migration_is_idempotent_and_preserves_history(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    stamp = "2025-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO research_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("old", "q", "a", "{}", "APPROVED", "PUBLIC", None, 0, stamp, stamp))
    conn.execute("INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ("review", "old", "reviewer_1", "approve", "", "[]", 0, None, stamp))
    conn.execute("INSERT INTO security_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("event", "old", "routing", "PUBLIC", "allow", "[]", None, None, "legacy", None, stamp))
    conn.commit()
    conn.close()
    for _ in range(2):
        repo = Repository(path)
        assert repo.policy_for_task("old").identity() == ("default", 1)
        assert repo.list_reviews_for_task("old")[0].policy_version == 1
        assert repo.get_security_event("event").policy_version == 1
        assert repo.get_task("old").created_at.isoformat() == stamp
        assert len(repo.list_policies()) == 1
        repo.close()


def test_read_only_api_exposes_active_versions_and_task_binding(repository, orchestrator):
    old = orchestrator.create_task("old", "a")
    install(repository, changed_policy())
    def override():
        repo = Repository(repository.db_path)
        try:
            yield repo
        finally:
            repo.close()
    app.dependency_overrides[get_repository] = override
    try:
        with TestClient(app) as client:
            assert client.get("/api/policies/active").json()["version"] == 2
            assert len(client.get("/api/policies").json()["policies"]) == 2
            assert client.get(f"/api/tasks/{old.task_id}/policy").json()["version"] == 1
            detail = client.get(f"/api/tasks/{old.task_id}").json()
            assert detail["policy_version"] == 1
            assert detail["publication_gate_policy"] == {"policy_id": "default", "version": 1}
            assert client.get("/api/tasks/missing/policy").status_code == 404
            assert client.post("/api/policies", json=DEFAULT.to_dict()).status_code == 405
    finally:
        app.dependency_overrides.clear()


def test_sql_replace_cannot_redefine_policy(repository):
    with pytest.raises(sqlite3.IntegrityError):
        repository._conn.execute("INSERT OR REPLACE INTO governance_policies VALUES (?, ?, ?)", ("default", 1, "{}"))
    assert repository.get_policy("default", 1) == DEFAULT


@pytest.mark.parametrize("configuration", ["A", "D", "F"])
def test_benchmark_result_policy_metadata(repository, configuration):
    from pathlib import Path
    from afra.benchmark.schema import load_benchmark
    from afra.benchmark.configurations import run_configuration_a, run_configuration_d, run_configuration_f
    suite = load_benchmark(Path(__file__).resolve().parents[1] / "benchmark" / "tasks_v1.json")
    task = suite.tasks[0]
    if configuration == "A":
        result = run_configuration_a(task)
        assert result.policy_id is None and result.policy_version is None
    else:
        install(repository, changed_policy())
        run = run_configuration_d if configuration == "D" else run_configuration_f
        result = run(task, repository)
        assert result.to_dict()["policy_id"] == "default"
        assert result.to_dict()["policy_version"] == 2
        assert repository.policy_for_task(result.research_task_id).version == 2


@pytest.mark.parametrize("status", [SupportStatus.UNSUPPORTED, SupportStatus.INSUFFICIENT_EVIDENCE])
def test_bad_evidence_still_abstains_under_assigned_policy(repository, orchestrator, status):
    install(repository, changed_policy())
    task = orchestrator.create_task("q", "analyst_1")
    task.state = TaskState.VALIDATING
    repository.save_task(task)
    make_claim(repository, task, status)
    assert orchestrator.evaluate_sufficiency_and_advance(task.task_id).state == TaskState.INSUFFICIENT_EVIDENCE
    assert not evaluate_publication_gate(task.task_id, repository).passed
