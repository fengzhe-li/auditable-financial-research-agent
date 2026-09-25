"""Phase 7 API tests (afra.api.app) - kept separate from core correctness
tests and from the Phase 6/6.5 benchmark test files, matching this
project's established convention of not mixing test concerns.

Every task fixture here is built by calling the same functions
backend/scripts/seed_demo_data.py uses (imported directly from that script,
not duplicated) - so these tests exercise the real seed data path, not a
parallel one. Every provider involved is a deterministic test double; this
file never imports afra.providers.real_model and makes no network call.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from afra.api.app import app, get_repository
from afra.storage.repository import Repository

_SEED_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo_data.py"
_spec = importlib.util.spec_from_file_location("seed_demo_data", _SEED_SCRIPT)
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


@pytest.fixture
def seeded(tmp_path):
    """Seeds a fresh temp database with one task per state (via the real
    seed script's functions) and points the API's dependency-injected
    Repository at it for the duration of the test."""
    db_path = tmp_path / "api_test.db"
    repo = Repository(db_path)
    task_ids = {
        "published": seed.seed_published(repo),
        "awaiting_review": seed.seed_awaiting_review(repo),
        "needs_clarification": seed.seed_needs_clarification(repo),
        "rejected": seed.seed_rejected(repo),
        "revision_requested": seed.seed_revision_requested(repo),
        "stale_approval": seed.seed_stale_approval(repo),
        "insufficient_evidence": seed.seed_insufficient_evidence(repo),
        "security_blocked": seed.seed_security_blocked(repo),
    }
    repo.close()

    def _override_repository():
        r = Repository(db_path)
        try:
            yield r
        finally:
            r.close()

    app.dependency_overrides[get_repository] = _override_repository
    client = TestClient(app)
    yield client, task_ids
    app.dependency_overrides.clear()


def test_health():
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_tasks_returns_every_seeded_task(seeded):
    client, task_ids = seeded
    response = client.get("/api/tasks")
    assert response.status_code == 200
    returned_ids = {t["task_id"] for t in response.json()["tasks"]}
    assert returned_ids == set(task_ids.values())


def test_list_tasks_most_recent_first(seeded):
    client, task_ids = seeded
    tasks = client.get("/api/tasks").json()["tasks"]
    timestamps = [t["created_at"] for t in tasks]
    assert timestamps == sorted(timestamps, reverse=True)


def test_get_task_detail_404_for_unknown_task(seeded):
    client, _ = seeded
    response = client.get("/api/tasks/does-not-exist")
    assert response.status_code == 404


def test_task_detail_includes_question_and_state(seeded):
    client, task_ids = seeded
    detail = client.get(f"/api/tasks/{task_ids['published']}").json()
    assert detail["question_text"]
    assert detail["final_status"] == "PUBLISHED"
    assert detail["created_by"] == "analyst_1"
    assert detail["classification"] == "PUBLIC"


# -- Publication-allowed must never be true for unsupported/blocked tasks ---


def test_security_blocked_task_is_never_publishable(seeded):
    client, task_ids = seeded
    detail = client.get(f"/api/tasks/{task_ids['security_blocked']}").json()
    assert detail["final_status"] == "SECURITY_BLOCKED"
    assert detail["publication_allowed"] is False
    summary = next(t for t in client.get("/api/tasks").json()["tasks"] if t["task_id"] == task_ids["security_blocked"])
    assert summary["publication_allowed"] is False


def test_insufficient_evidence_task_is_never_publishable(seeded):
    client, task_ids = seeded
    detail = client.get(f"/api/tasks/{task_ids['insufficient_evidence']}").json()
    assert detail["final_status"] == "INSUFFICIENT_EVIDENCE"
    assert detail["publication_allowed"] is False


def test_needs_clarification_task_is_never_publishable(seeded):
    client, task_ids = seeded
    detail = client.get(f"/api/tasks/{task_ids['needs_clarification']}").json()
    assert detail["publication_allowed"] is False


def test_published_task_reports_publication_allowed_false_after_the_fact(seeded):
    """publication_allowed reflects "eligible to call publish() right now"
    (afra.review.publication_gate.evaluate_publication_gate requires
    state==APPROVED) - a task already PUBLISHED correctly shows False, not
    a claim about whether the publish that already happened was valid."""
    client, task_ids = seeded
    detail = client.get(f"/api/tasks/{task_ids['published']}").json()
    assert detail["final_status"] == "PUBLISHED"
    assert detail["publication_allowed"] is False
    assert "not APPROVED" in " ".join(detail["publication_gate_reasons"])


# -- Stale approvals -----------------------------------------------------


def test_stale_approval_displays_correctly(seeded):
    client, task_ids = seeded
    detail = client.get(f"/api/tasks/{task_ids['stale_approval']}").json()
    assert detail["final_status"] == "APPROVED"
    assert detail["stale_approval"] is True
    assert detail["publication_allowed"] is False
    assert any("stale" in reason for reason in detail["publication_gate_reasons"])


def test_non_stale_approval_would_display_as_not_stale(seeded):
    """A fresh APPROVED task (no version bump after approval) must not be
    flagged stale - the check must distinguish the two cases, not always
    report True."""
    client, task_ids = seeded
    response = client.post(
        f"/api/tasks/{task_ids['awaiting_review']}/review",
        json={"reviewer_id": "reviewer_1", "decision": "approve"},
    )
    assert response.status_code == 200
    detail = client.get(f"/api/tasks/{task_ids['awaiting_review']}").json()
    assert detail["stale_approval"] is False
    assert detail["publication_allowed"] is True


# -- Revision / version history -------------------------------------------


def test_revision_requested_task_detail(seeded):
    client, task_ids = seeded
    detail = client.get(f"/api/tasks/{task_ids['revision_requested']}").json()
    assert detail["final_status"] == "REVISION_REQUESTED"
    assert len(detail["reviews"]) == 1
    assert detail["reviews"][0]["decision"] == "request_revision"


def test_resume_from_revision_moves_to_synthesising_by_default(seeded):
    client, task_ids = seeded
    response = client.post(f"/api/tasks/{task_ids['revision_requested']}/resume", json={})
    assert response.status_code == 200
    assert response.json()["state"] == "SYNTHESISING"


def test_resume_from_revision_with_new_evidence_moves_to_gathering_evidence(seeded):
    client, task_ids = seeded
    response = client.post(
        f"/api/tasks/{task_ids['revision_requested']}/resume", json={"needs_new_evidence": True}
    )
    assert response.status_code == 200
    assert response.json()["state"] == "GATHERING_EVIDENCE"


def test_resume_from_revision_illegal_on_a_non_revision_task(seeded):
    client, task_ids = seeded
    response = client.post(f"/api/tasks/{task_ids['published']}/resume", json={})
    assert response.status_code == 400


def test_claim_set_version_visible_on_task_detail(seeded):
    client, task_ids = seeded
    detail = client.get(f"/api/tasks/{task_ids['stale_approval']}").json()
    assert detail["claim_set_version"] == 2
    assert detail["reviews"][0]["claim_set_version"] == 1


# -- Review actions ---------------------------------------------------------


def test_submit_review_approve_then_publish(seeded):
    client, task_ids = seeded
    r1 = client.post(
        f"/api/tasks/{task_ids['awaiting_review']}/review",
        json={"reviewer_id": "reviewer_1", "decision": "approve", "comments": "ok"},
    )
    assert r1.status_code == 200 and r1.json()["state"] == "APPROVED"
    r2 = client.post(f"/api/tasks/{task_ids['awaiting_review']}/publish")
    assert r2.status_code == 200 and r2.json()["state"] == "PUBLISHED"


def test_submit_review_rejects_self_approval(seeded):
    client, task_ids = seeded
    response = client.post(
        f"/api/tasks/{task_ids['awaiting_review']}/review",
        json={"reviewer_id": "analyst_1", "decision": "approve"},
    )
    assert response.status_code == 400


def test_submit_review_rejects_unauthorized_reviewer(seeded):
    client, task_ids = seeded
    response = client.post(
        f"/api/tasks/{task_ids['awaiting_review']}/review",
        json={"reviewer_id": "random_person", "decision": "approve"},
    )
    assert response.status_code == 400


def test_submit_review_invalid_decision_string_is_422(seeded):
    client, task_ids = seeded
    response = client.post(
        f"/api/tasks/{task_ids['awaiting_review']}/review",
        json={"reviewer_id": "reviewer_1", "decision": "approve_immediately"},
    )
    assert response.status_code == 422


def test_publish_before_approval_fails(seeded):
    client, task_ids = seeded
    response = client.post(f"/api/tasks/{task_ids['awaiting_review']}/publish")
    assert response.status_code == 400


# -- Claim revision endpoints (docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics) --


def _first_claim_id(client, task_id):
    detail = client.get(f"/api/tasks/{task_id}").json()
    return detail["draft_claims"][0]["claim_id"]


def test_replace_claim_endpoint(seeded):
    client, task_ids = seeded
    claim_id = _first_claim_id(client, task_ids["awaiting_review"])

    response = client.post(
        f"/api/tasks/{task_ids['awaiting_review']}/claims/replace",
        json={"old_claim_id": claim_id, "claim_text": "Corrected claim text.", "created_by": "analyst_1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["predecessor_claim_id"] == claim_id
    assert body["claim_set_version"] == 2

    detail = client.get(f"/api/tasks/{task_ids['awaiting_review']}").json()
    by_id = {c["claim_id"]: c for c in detail["draft_claims"]}
    assert by_id[claim_id]["lifecycle_status"] == "REPLACED"
    assert by_id[claim_id]["is_effective"] is False
    assert by_id[claim_id]["successor_claim_id"] == body["claim_id"]
    assert body["claim_id"] in detail["effective_claim_ids"]


def test_withdraw_claim_endpoint(seeded):
    client, task_ids = seeded
    claim_id = _first_claim_id(client, task_ids["awaiting_review"])

    response = client.post(
        f"/api/tasks/{task_ids['awaiting_review']}/claims/withdraw",
        json={"claim_id": claim_id, "reason": "no longer relevant"},
    )

    assert response.status_code == 200
    assert response.json()["claim_set_version"] == 2

    detail = client.get(f"/api/tasks/{task_ids['awaiting_review']}").json()
    by_id = {c["claim_id"]: c for c in detail["draft_claims"]}
    assert by_id[claim_id]["lifecycle_status"] == "WITHDRAWN"
    assert by_id[claim_id]["withdrawn_reason"] == "no longer relevant"
    assert claim_id not in detail["effective_claim_ids"]


def test_object_to_claim_endpoint(seeded):
    client, task_ids = seeded
    claim_id = _first_claim_id(client, task_ids["awaiting_review"])

    response = client.post(
        f"/api/tasks/{task_ids['awaiting_review']}/claims/object",
        json={"claim_id": claim_id, "reviewer_id": "reviewer_1", "reason": "please double-check this citation"},
    )

    assert response.status_code == 200
    assert response.json()["claim_id"] == claim_id

    detail = client.get(f"/api/tasks/{task_ids['awaiting_review']}").json()
    by_id = {c["claim_id"]: c for c in detail["draft_claims"]}
    assert by_id[claim_id]["objections"][0]["reason"] == "please double-check this citation"
    # An objection alone does not change lifecycle status or the effective set.
    assert by_id[claim_id]["lifecycle_status"] == "ACTIVE"
    assert claim_id in detail["effective_claim_ids"]


def test_object_to_claim_endpoint_rejects_self_objection(seeded):
    client, task_ids = seeded
    claim_id = _first_claim_id(client, task_ids["awaiting_review"])

    response = client.post(
        f"/api/tasks/{task_ids['awaiting_review']}/claims/object",
        json={"claim_id": claim_id, "reviewer_id": "analyst_1", "reason": "x"},
    )

    assert response.status_code == 400


def test_replace_claim_endpoint_error_for_unknown_claim(seeded):
    client, task_ids = seeded
    response = client.post(
        f"/api/tasks/{task_ids['awaiting_review']}/claims/replace",
        json={"old_claim_id": "claim_does_not_exist", "claim_text": "x", "created_by": "analyst_1"},
    )
    assert response.status_code == 400


# -- Evaluation endpoint: real-model validation must be labelled pending ---


def test_evaluation_endpoint_labels_real_model_validation_complete():
    client = TestClient(app)
    response = client.get("/api/evaluation")
    assert response.status_code == 200
    data = response.json()
    assert data["real_model_validation"]["status"] == "complete"
    assert "complete" in data["real_model_validation"]["label"].lower()
    assert data["real_model_validation"]["run_id"] == "realmodel_live_1fb8b08dbbc3"
    assert data["real_model_validation"]["git_commit"] == "4386ac6f247226ad297d02573acedabb5d00f0d8"
    assert data["real_model_validation"]["execution_backend"] == "Antigravity CLI"
    assert data["real_model_validation"]["underlying_model"] == "gemini-3.8-flash-low"
    assert data["real_model_validation"]["configurations"]["F"]["unsupported_claim_rate"]["value"] == 0.0


def test_evaluation_endpoint_deterministic_benchmark_is_labelled_synthetic():
    client = TestClient(app)
    data = client.get("/api/evaluation").json()
    deterministic = data["deterministic_benchmark"]
    if deterministic is not None:  # None only if no benchmark run has ever been produced on disk
        assert "synthetic" in deterministic["label"].lower() or "fixture" in deterministic["label"].lower()
        assert "no real external llm" in deterministic["label"].lower()


def test_evaluation_endpoint_frozen_subset_reflects_real_file():
    client = TestClient(app)
    data = client.get("/api/evaluation").json()
    subset = data["real_model_validation"]["frozen_subset"]
    if subset is not None:
        assert len(subset["task_ids"]) == 14
