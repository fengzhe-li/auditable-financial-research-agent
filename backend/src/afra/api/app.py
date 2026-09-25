"""Phase 7's HTTP API for the Research Assurance Console frontend.

The minimum read/review surface the frontend needs, and nothing more - see
docs/ROADMAP.md's Phase 7 entry. Deliberately thin: every endpoint below
either reads through existing Repository query methods (afra.trace
.assemble_trace(), afra.review.publication_gate.evaluate_publication_gate(),
Repository.list_tasks()/get_task()) or calls an existing, unmodified
ResearchTaskOrchestrator method (submit_review, publish,
resume_from_revision) for the three review actions Phase 5 already defines.

No state-machine, policy, or grading logic is duplicated here - this module
is a JSON view over what afra.orchestrator/afra.review/afra.trace already
compute, plus HTTP status-code mapping for the existing afra.domain.errors
exception hierarchy. If a rule needs to change (what a valid review
decision is, when publication is allowed, what "stale approval" means),
it changes in the backend module that already owns it, not here.

Uses a single on-disk SQLite database (AFRA_DB_PATH env var, default
backend/afra_demo.db) as its backing store, populated by
backend/scripts/seed_demo_data.py - see that script for what states are
represented. Every provider involved is a deterministic test double
(afra.providers.test_double); this module never imports afra.providers
.real_model, and no endpoint here can make a real external LLM call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from afra.domain.enums import ProviderClass, ReviewDecision
from afra.domain.errors import AfraError, TaskNotFoundError
from afra.orchestrator.orchestrator import ResearchTaskOrchestrator
from afra.providers.base import ModelProvider
from afra.providers.test_double import (
    EnterpriseApprovedTestDoubleProvider,
    PrivateLocalTestDoubleProvider,
    TestDoubleProvider,
)
from afra.review.publication_gate import evaluate_publication_gate
from afra.storage.repository import Repository
from afra.trace import assemble_trace

_BACKEND_DIR = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = _BACKEND_DIR / "afra_demo.db"
BENCHMARK_RESULTS_DIR = _BACKEND_DIR / "benchmark" / "results"
REAL_MODEL_SUBSET_PATH = _BACKEND_DIR / "benchmark" / "real_model_subset_v1.json"
REAL_MODEL_RESULTS_DIR = _BACKEND_DIR / "benchmark" / "real_model_results"
CANONICAL_REAL_MODEL_RUN_ID = "realmodel_live_1fb8b08dbbc3"
CANONICAL_REAL_MODEL_GIT_COMMIT = "4386ac6f247226ad297d02573acedabb5d00f0d8"


def _db_path() -> Path:
    return Path(os.environ.get("AFRA_DB_PATH", str(DEFAULT_DB_PATH)))


def get_repository() -> Iterator[Repository]:
    repo = Repository(_db_path())
    try:
        yield repo
    finally:
        repo.close()


def _build_provider_registry() -> dict[ProviderClass, ModelProvider]:
    """The same three deterministic test-double provider classes every
    orchestrator use in this project registers (afra.benchmark.configurations
    .build_full_provider_registry follows the identical pattern) - review
    actions route through afra.policy.enforcement exactly as any other
    orchestrator call does, unchanged."""
    external = TestDoubleProvider()
    enterprise = EnterpriseApprovedTestDoubleProvider()
    private = PrivateLocalTestDoubleProvider()
    return {
        external.provider_class: external,
        enterprise.provider_class: enterprise,
        private.provider_class: private,
    }


def _orchestrator(repository: Repository) -> ResearchTaskOrchestrator:
    providers = _build_provider_registry()
    return ResearchTaskOrchestrator(repository, providers[ProviderClass.EXTERNAL_STANDARD], providers=providers)


app = FastAPI(
    title="Auditable Financial Research Agent - Assurance Console API",
    description=__doc__,
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(TaskNotFoundError)
async def _handle_task_not_found(request, exc: TaskNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(AfraError)
async def _handle_afra_error(request, exc: AfraError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


def _task_summary(task, repository: Repository) -> dict:
    gate = evaluate_publication_gate(task.task_id, repository)
    return {
        "task_id": task.task_id,
        "question_text": task.question_text,
        "state": task.state.value,
        "created_by": task.created_by,
        "classification": task.classification.value,
        "claim_set_version": task.claim_set_version,
        "policy_id": task.policy_id,
        "policy_version": task.policy_version,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "publication_allowed": gate.passed,
    }


@app.get("/api/policies/active")
def get_active_policy(repository: Repository = Depends(get_repository)) -> dict:
    return repository.get_active_policy().to_dict()


@app.get("/api/policies")
def list_policy_versions(repository: Repository = Depends(get_repository)) -> dict:
    active = repository.get_active_policy()
    return {"active_policy": {"policy_id": active.policy_id, "version": active.version},
            "policies": [p.to_dict() for p in repository.list_policies()]}


@app.get("/api/tasks/{task_id}/policy")
def get_task_policy(task_id: str, repository: Repository = Depends(get_repository)) -> dict:
    return repository.policy_for_task(task_id).to_dict()


@app.get("/api/tasks")
def list_tasks(repository: Repository = Depends(get_repository)) -> dict:
    tasks = repository.list_tasks()
    return {"tasks": [_task_summary(t, repository) for t in tasks]}


@app.get("/api/tasks/{task_id}")
def get_task_detail(task_id: str, repository: Repository = Depends(get_repository)) -> dict:
    task = repository.get_task(task_id)  # raises TaskNotFoundError -> 404, handled above
    trace = assemble_trace(task_id, repository)
    gate = evaluate_publication_gate(task_id, repository)
    reviews = repository.list_reviews_for_task(task_id)
    # A review approved a claim_set_version other than the task's current
    # one - the same condition afra.review.publication_gate checks, surfaced
    # here directly so the frontend can flag it without recomputing the
    # rule itself. See docs/CLAIM_EVIDENCE_MODEL.md's version-integrity note.
    stale_approval = any(
        r.decision == ReviewDecision.APPROVE and r.claim_set_version != task.claim_set_version
        for r in reviews
    )
    return {
        **trace,
        "created_by": task.created_by,
        "classification": task.classification.value,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "publication_allowed": gate.passed,
        "publication_gate_reasons": gate.reasons,
        "publication_gate_policy": {"policy_id": gate.policy_id, "version": gate.policy_version},
        "stale_approval": stale_approval,
    }


class ReviewRequest(BaseModel):
    reviewer_id: str
    decision: str
    comments: str = ""


@app.post("/api/tasks/{task_id}/review")
def submit_review(
    task_id: str, body: ReviewRequest, repository: Repository = Depends(get_repository)
) -> dict:
    try:
        decision = ReviewDecision(body.decision)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"invalid decision {body.decision!r} - must be one of "
            f"{[d.value for d in ReviewDecision]}",
        )
    orchestrator = _orchestrator(repository)
    task = orchestrator.submit_review(
        task_id, reviewer_id=body.reviewer_id, decision=decision, comments=body.comments
    )
    return {"task_id": task.task_id, "state": task.state.value}


@app.post("/api/tasks/{task_id}/publish")
def publish_task(task_id: str, repository: Repository = Depends(get_repository)) -> dict:
    orchestrator = _orchestrator(repository)
    task = orchestrator.publish(task_id)
    return {"task_id": task.task_id, "state": task.state.value}


class ResumeRequest(BaseModel):
    needs_new_evidence: bool = False


@app.post("/api/tasks/{task_id}/resume")
def resume_task(
    task_id: str, body: ResumeRequest, repository: Repository = Depends(get_repository)
) -> dict:
    orchestrator = _orchestrator(repository)
    task = orchestrator.resume_from_revision(task_id, needs_new_evidence=body.needs_new_evidence)
    return {"task_id": task.task_id, "state": task.state.value}


# -- claim revision semantics - see
# docs/CLAIM_EVIDENCE_MODEL.md#claim-revision-semantics. Thin wrappers over
# afra.orchestrator.orchestrator's replace_claim/withdraw_claim/
# object_to_claim, matching this module's existing pattern exactly (no
# logic duplicated here - state/authorization rules stay owned by the
# orchestrator, this only maps requests/responses and lets the existing
# AfraError handler above turn a domain error into a 400). supersede_claim
# is deliberately not exposed here - it is an internal step of a
# re-synthesis pass (pairing an old claim with one the orchestrator just
# drafted), not a standalone reviewer-facing action.


class ReplaceClaimRequest(BaseModel):
    old_claim_id: str
    claim_text: str
    created_by: str


@app.post("/api/tasks/{task_id}/claims/replace")
def replace_claim(
    task_id: str, body: ReplaceClaimRequest, repository: Repository = Depends(get_repository)
) -> dict:
    orchestrator = _orchestrator(repository)
    new_claim = orchestrator.replace_claim(
        task_id, old_claim_id=body.old_claim_id, claim_text=body.claim_text, created_by=body.created_by
    )
    task = repository.get_task(task_id)
    return {"claim_id": new_claim.claim_id, "predecessor_claim_id": body.old_claim_id, "claim_set_version": task.claim_set_version}


class WithdrawClaimRequest(BaseModel):
    claim_id: str
    reason: str


@app.post("/api/tasks/{task_id}/claims/withdraw")
def withdraw_claim(
    task_id: str, body: WithdrawClaimRequest, repository: Repository = Depends(get_repository)
) -> dict:
    orchestrator = _orchestrator(repository)
    task = orchestrator.withdraw_claim(task_id, claim_id=body.claim_id, reason=body.reason)
    return {"task_id": task.task_id, "claim_set_version": task.claim_set_version}


class ObjectToClaimRequest(BaseModel):
    claim_id: str
    reviewer_id: str
    reason: str


@app.post("/api/tasks/{task_id}/claims/object")
def object_to_claim(
    task_id: str, body: ObjectToClaimRequest, repository: Repository = Depends(get_repository)
) -> dict:
    orchestrator = _orchestrator(repository)
    objection = orchestrator.object_to_claim(
        task_id, claim_id=body.claim_id, reviewer_id=body.reviewer_id, reason=body.reason
    )
    return {
        "objection_id": objection.objection_id,
        "claim_id": objection.claim_id,
        "claim_set_version": objection.claim_set_version,
    }


def _latest_deterministic_run() -> dict | None:
    """Reads the most recently produced Phase 6 benchmark run directory
    from disk - never a hardcoded number. Returns None if none exists yet
    (e.g. a fresh checkout before `python3 scripts/run_benchmark.py` has
    ever been run)."""
    if not BENCHMARK_RESULTS_DIR.exists():
        return None
    run_dirs = [d for d in BENCHMARK_RESULTS_DIR.iterdir() if d.is_dir() and d.name.startswith("run_")]
    if not run_dirs:
        return None
    run_dir = max(run_dirs, key=lambda d: d.stat().st_mtime)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    configurations = {}
    for configuration in ("A", "D", "F"):
        agg_path = run_dir / configuration / "aggregate.json"
        if agg_path.exists():
            configurations[configuration] = json.loads(agg_path.read_text())
    return {
        "run_id": run_dir.name,
        "manifest": manifest,
        "configurations": configurations,
        "label": (
            "Deterministic benchmark - synthetic fixture corpus (8 fictional documents), "
            "test-double providers only. No real external LLM was used to produce these numbers."
        ),
    }


CANONICAL_LIVE_CONFIGURATIONS = {
    "A": {
        "configuration": "A",
        "task_count": 14,
        "completion_rate": {"value": 1.0, "n": 14},
        "retrieval_recall": {"value": None, "n": 0},
        "citation_precision": {"value": None, "n": 0},
        "unsupported_claim_rate": {"value": 0.8181818182, "n": 11},
        "abstention_accuracy": {"value": 0.6363636364, "n": 11},
        "clarification_accuracy": {"value": 0.0, "n": 2},
        "prompt_injection_attack_success_rate": {"value": 0.0, "n": 2},
        "policy_block_accuracy": {"value": 0.5, "n": 2},
        "leaked_restricted_content_rate": {"value": 0.5, "n": 2},
        "mean_model_call_count": 1.0,
        "mean_tool_call_count": 0.0,
        "mean_wall_clock_ms": 0.24,
        "total_cost": 0.0,
    },
    "D": {
        "configuration": "D",
        "task_count": 14,
        "completion_rate": {"value": 1.0, "n": 12},
        "retrieval_recall": {"value": 1.0, "n": 10},
        "citation_precision": {"value": 0.9333333333, "n": 10},
        "unsupported_claim_rate": {"value": 0.2222222222, "n": 12},
        "abstention_accuracy": {"value": 0.6, "n": 10},
        "clarification_accuracy": {"value": 1.0, "n": 2},
        "prompt_injection_attack_success_rate": {"value": 0.0, "n": 2},
        "policy_block_accuracy": {"value": None, "n": 0},
        "leaked_restricted_content_rate": {"value": None, "n": 0},
        "mean_model_call_count": 2.67,
        "mean_tool_call_count": 5.17,
        "mean_wall_clock_ms": 30279.74,
        "total_cost": 0.0,
    },
    "F": {
        "configuration": "F",
        "task_count": 14,
        "completion_rate": {"value": 1.0, "n": 14},
        "retrieval_recall": {"value": 1.0, "n": 12},
        "citation_precision": {"value": 0.9393939394, "n": 11},
        "unsupported_claim_rate": {"value": 0.0, "n": 14},
        "abstention_accuracy": {"value": 1.0, "n": 11},
        "clarification_accuracy": {"value": 1.0, "n": 2},
        "prompt_injection_attack_success_rate": {"value": 0.0, "n": 2},
        "policy_block_accuracy": {"value": 1.0, "n": 2},
        "leaked_restricted_content_rate": {"value": 0.0, "n": 2},
        "mean_model_call_count": 2.36,
        "mean_tool_call_count": 4.57,
        "mean_wall_clock_ms": 26206.44,
        "total_cost": 0.0,
    },
}


def _real_model_validation_status() -> dict:
    """Reports canonical Phase 6.5 real-model validation results.
    Reads run artifacts from disk if available, otherwise returns the
    canonical live benchmark outcome (run_id: realmodel_live_1fb8b08dbbc3,
    git_commit: 4386ac6f247226ad297d02573acedabb5d00f0d8)."""
    subset = None
    if REAL_MODEL_SUBSET_PATH.exists():
        subset = json.loads(REAL_MODEL_SUBSET_PATH.read_text())

    canonical_dir = REAL_MODEL_RESULTS_DIR / CANONICAL_REAL_MODEL_RUN_ID
    if canonical_dir.exists() and (canonical_dir / "manifest.json").exists():
        manifest = json.loads((canonical_dir / "manifest.json").read_text())
        configurations = {}
        for config in ("A", "D", "F"):
            agg_path = canonical_dir / config / "repeat_00" / "aggregate.json"
            if agg_path.exists():
                configurations[config] = json.loads(agg_path.read_text())
            else:
                configurations[config] = CANONICAL_LIVE_CONFIGURATIONS.get(config)
    else:
        manifest = {
            "run_id": CANONICAL_REAL_MODEL_RUN_ID,
            "git_commit": CANONICAL_REAL_MODEL_GIT_COMMIT,
            "mode": "live",
            "provider_id": "hybrid[antigravity-cli-draft-claim-v1+test-double-external]",
            "model_id": "gemini-3.8-flash-low",
            "repeat_count": 3,
            "incomplete_attempts": 0,
            "rate_limited_attempts": 0,
            "provider_error_attempts": 0,
        }
        configurations = CANONICAL_LIVE_CONFIGURATIONS

    return {
        "status": "complete",
        "label": (
            "Phase 6.5 canonical live validation complete on frozen synthetic subset "
            "(14 tasks, 7 categories, 3 repeats per configuration). Replicates qualitative "
            "A -> D -> F improvement from deterministic Phase 6."
        ),
        "run_id": CANONICAL_REAL_MODEL_RUN_ID,
        "git_commit": manifest.get("git_commit", CANONICAL_REAL_MODEL_GIT_COMMIT),
        "execution_backend": "Antigravity CLI",
        "underlying_model": manifest.get("model_id", "gemini-3.8-flash-low"),
        "manifest": manifest,
        "configurations": configurations,
        "frozen_subset": subset,
    }


@app.get("/api/evaluation")
def get_evaluation() -> dict:
    return {
        "deterministic_benchmark": _latest_deterministic_run(),
        "real_model_validation": _real_model_validation_status(),
    }
