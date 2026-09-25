"""SQLite-backed persistence for the Phase 1 vertical slice.

Real, on-disk, process-restart-durable storage - see the Phase 1 deviation
note in /docs/ROADMAP.md#recording-deviations for why SQLite rather than the
documented PostgreSQL target is used here, and what would need to change to
move to Postgres later (schema is written to map directly onto the entities
in /docs/CLAIM_EVIDENCE_MODEL.md, so the move is a storage-layer swap).

This module is the single place that writes Claim.support_status /
support_strength / validator_result_id - see
docs/CLAIM_EVIDENCE_MODEL.md#validation-authority. No other code path may
set those fields directly.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from afra.domain.enums import (
    ApprovalStatus,
    Classification,
    ClaimLifecycleStatus,
    DLPAction,
    ReviewDecision,
    SupportContribution,
    SupportStatus,
    TaskState,
)
from afra.domain.errors import TaskNotFoundError, InvalidPolicyError, PolicyNotFoundError, PolicyVersionConflictError
from afra.domain.enums import PolicyStatus
from afra.governance.policy import GovernancePolicy, validate_policy_or_raise
from afra.governance.default_policy import DEFAULT_GOVERNANCE_POLICY
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
    ValidationResult,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_tasks (
    task_id TEXT PRIMARY KEY,
    question_text TEXT NOT NULL,
    created_by TEXT NOT NULL,
    resolved_scope TEXT NOT NULL,
    state TEXT NOT NULL,
    classification TEXT NOT NULL,
    pending_clarification TEXT,
    claim_set_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(task_id),
    source_document_id TEXT NOT NULL,
    source_location TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    source_timestamp TEXT,
    retrieved_at TEXT NOT NULL,
    retrieval_tool TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    classification TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(task_id),
    claim_text TEXT NOT NULL,
    model_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    support_status TEXT,
    support_strength REAL,
    validator_result_id TEXT,
    source_timestamp TEXT,
    reviewer_id TEXT,
    approval_status TEXT NOT NULL,
    subquestion TEXT,
    claim_set_version INTEGER NOT NULL DEFAULT 0,
    lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE',
    predecessor_claim_id TEXT,
    successor_claim_id TEXT,
    withdrawn_reason TEXT
);

CREATE TABLE IF NOT EXISTS claim_objections (
    objection_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(task_id),
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    reviewer_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    claim_set_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_evidence_links (
    link_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    quote_span TEXT NOT NULL,
    support_contribution TEXT NOT NULL,
    relevance_score REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_results (
    validation_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    computed_support_status TEXT NOT NULL,
    computed_support_strength REAL NOT NULL,
    citation_check_passed INTEGER NOT NULL,
    conflicting_evidence_ids TEXT NOT NULL,
    missing_evidence_description TEXT,
    validator_model_id TEXT NOT NULL,
    validated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(task_id),
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    comments TEXT NOT NULL,
    claims_reviewed TEXT NOT NULL,
    claim_set_version INTEGER NOT NULL DEFAULT 0,
    security_context TEXT,
    reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_calls (
    model_call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(task_id),
    purpose TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    routing_classification TEXT NOT NULL,
    input_summary TEXT NOT NULL,
    output_summary TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    cost REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(task_id),
    tool_name TEXT NOT NULL,
    input_summary TEXT NOT NULL,
    output_summary TEXT NOT NULL,
    succeeded INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_transitions (
    transition_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(task_id),
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS security_events (
    security_event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(task_id),
    trigger TEXT NOT NULL,
    classification TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    detected_categories TEXT NOT NULL,
    requested_provider_id TEXT,
    selected_provider_id TEXT,
    policy_result TEXT NOT NULL,
    resolved_by TEXT,
    created_at TEXT NOT NULL
);
"""


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Repository:
    """Owns the SQLite connection and all read/write access to persisted
    state. The orchestrator, validator, and review service depend on this
    interface, not on sqlite3 directly.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._migrate_claims_table_for_revision_semantics()
        self._migrate_governance()
        self._conn.commit()

    def _migrate_claims_table_for_revision_semantics(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` (in SCHEMA above) never alters an
        existing table - a `claims` table created before the revision-
        semantics columns existed (e.g. an already-seeded
        backend/afra_demo.db from an earlier run) would otherwise be
        missing them forever. This adds any that are absent, defaulted
        exactly as SCHEMA declares them, so an existing on-disk database
        keeps working without a manual migration step. A no-op on any
        database that already has them (every fresh one, from this
        module's own SCHEMA).
        """
        existing_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(claims)").fetchall()}
        migrations = {
            "claim_set_version": "ALTER TABLE claims ADD COLUMN claim_set_version INTEGER NOT NULL DEFAULT 0",
            "lifecycle_status": "ALTER TABLE claims ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE'",
            "predecessor_claim_id": "ALTER TABLE claims ADD COLUMN predecessor_claim_id TEXT",
            "successor_claim_id": "ALTER TABLE claims ADD COLUMN successor_claim_id TEXT",
            "withdrawn_reason": "ALTER TABLE claims ADD COLUMN withdrawn_reason TEXT",
        }
        for column, statement in migrations.items():
            if column not in existing_columns:
                self._conn.execute(statement)

    def _migrate_governance(self) -> None:
        # Additive migrations: historical tasks/reviews/routing used default v1.
        for table in ("research_tasks", "reviews", "security_events"):
            columns = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in (("policy_id", "TEXT NOT NULL DEFAULT 'default'"),
                                      ("policy_version", "INTEGER NOT NULL DEFAULT 1")):
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS governance_policies (
                policy_id TEXT NOT NULL, version INTEGER NOT NULL,
                specification TEXT NOT NULL, PRIMARY KEY (policy_id, version)
            );
            CREATE TABLE IF NOT EXISTS active_governance_policy (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                policy_id TEXT NOT NULL, version INTEGER NOT NULL,
                FOREIGN KEY (policy_id, version) REFERENCES governance_policies(policy_id, version)
            );
            CREATE TRIGGER IF NOT EXISTS governance_no_replace
            BEFORE INSERT ON governance_policies
            WHEN EXISTS (SELECT 1 FROM governance_policies WHERE policy_id = NEW.policy_id AND version = NEW.version)
            BEGIN SELECT RAISE(ABORT, 'policy version already exists'); END;
            CREATE TRIGGER IF NOT EXISTS governance_no_update
            BEFORE UPDATE ON governance_policies BEGIN
                SELECT RAISE(ABORT, 'policy versions are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS governance_no_delete
            BEFORE DELETE ON governance_policies BEGIN
                SELECT RAISE(ABORT, 'policy versions are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS task_policy_no_rebind
            BEFORE UPDATE OF policy_id, policy_version ON research_tasks
            WHEN OLD.policy_id != NEW.policy_id OR OLD.policy_version != NEW.policy_version
            BEGIN SELECT RAISE(ABORT, 'task policy binding is immutable'); END;
            CREATE TABLE IF NOT EXISTS publication_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES research_tasks(task_id),
                policy_id TEXT NOT NULL, policy_version INTEGER NOT NULL,
                claim_set_version INTEGER NOT NULL, passed INTEGER NOT NULL,
                reasons TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY (policy_id, policy_version) REFERENCES governance_policies(policy_id, version)
            );
        """)
        default = DEFAULT_GOVERNANCE_POLICY
        self._conn.execute(
            "INSERT INTO governance_policies SELECT ?, ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM governance_policies WHERE policy_id = ? AND version = ?)",
            (*default.identity(), json.dumps(default.to_dict(), sort_keys=True), *default.identity()),
        )
        self._conn.execute("INSERT OR IGNORE INTO active_governance_policy VALUES (1, ?, ?)", default.identity())

    def save_policy(self, policy: GovernancePolicy) -> None:
        """Register a new immutable identity; never upsert or auto-activate."""
        validate_policy_or_raise(policy)
        try:
            with self._conn:
                self._conn.execute("INSERT INTO governance_policies VALUES (?, ?, ?)",
                                   (*policy.identity(), json.dumps(policy.to_dict(), sort_keys=True)))
        except sqlite3.IntegrityError as exc:
            raise PolicyVersionConflictError(f"policy version already exists: {policy.identity()}") from exc

    def get_policy(self, policy_id: str, version: int) -> GovernancePolicy:
        row = self._conn.execute(
            "SELECT specification FROM governance_policies WHERE policy_id = ? AND version = ?",
            (policy_id, version),
        ).fetchone()
        if row is None:
            raise PolicyNotFoundError(f"policy {policy_id!r} v{version} not found")
        return GovernancePolicy.from_dict(json.loads(row[0]))

    def list_policies(self) -> list[GovernancePolicy]:
        return [GovernancePolicy.from_dict(json.loads(r[0])) for r in self._conn.execute(
            "SELECT specification FROM governance_policies ORDER BY policy_id, version")]

    def get_active_policy(self) -> GovernancePolicy:
        row = self._conn.execute("SELECT policy_id, version FROM active_governance_policy WHERE singleton = 1").fetchone()
        return self.get_policy(*row)

    def activate_policy(self, policy_id: str, version: int) -> None:
        policy = self.get_policy(policy_id, version)
        if policy.status != PolicyStatus.ACTIVE:
            raise InvalidPolicyError("only an ACTIVE specification can be selected")
        if policy.effective_from and policy.effective_from > datetime.now(timezone.utc):
            raise InvalidPolicyError("policy is not yet effective")
        with self._conn:
            self._conn.execute("UPDATE active_governance_policy SET policy_id = ?, version = ? WHERE singleton = 1",
                               policy.identity())

    def policy_for_task(self, task_id: str) -> GovernancePolicy:
        task = self.get_task(task_id)
        return self.get_policy(task.policy_id, task.policy_version)

    def record_publication_decision(self, task: ResearchTask, result) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO publication_decisions
                (task_id, policy_id, policy_version, claim_set_version, passed, reasons, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task.task_id, result.policy_id, result.policy_version, task.claim_set_version,
                 int(result.passed), json.dumps(result.reasons), datetime.now(timezone.utc).isoformat()),
            )

    def list_publication_decisions(self, task_id: str) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM publication_decisions WHERE task_id = ? ORDER BY decision_id", (task_id,))
        columns = [c[0] for c in cur.description]
        results = []
        for row in cur:
            result = dict(zip(columns, row))
            result["passed"] = bool(result["passed"])
            result["reasons"] = json.loads(result["reasons"])
            results.append(result)
        return results

    def close(self) -> None:
        self._conn.close()

    # -- ResearchTask ---------------------------------------------------

    def save_task(self, task: ResearchTask) -> None:
        self.get_policy(task.policy_id, task.policy_version)
        existing = self._conn.execute("SELECT policy_id, policy_version FROM research_tasks WHERE task_id = ?", (task.task_id,)).fetchone()
        if existing is not None and existing != (task.policy_id, task.policy_version):
            raise PolicyVersionConflictError("an existing task cannot be rebound to another policy version")
        self._conn.execute(
            """
            INSERT INTO research_tasks
                (task_id, question_text, created_by, resolved_scope, state,
                 classification, pending_clarification, claim_set_version,
                 created_at, updated_at, policy_id, policy_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                resolved_scope=excluded.resolved_scope,
                state=excluded.state,
                classification=excluded.classification,
                pending_clarification=excluded.pending_clarification,
                claim_set_version=excluded.claim_set_version,
                updated_at=excluded.updated_at
            """,
            (
                task.task_id,
                task.question_text,
                task.created_by,
                json.dumps(task.resolved_scope),
                task.state.value,
                task.classification.value,
                task.pending_clarification,
                task.claim_set_version,
                _dt(task.created_at),
                _dt(task.updated_at),
                task.policy_id,
                task.policy_version,
            ),
        )
        self._conn.commit()

    def get_task(self, task_id: str) -> ResearchTask:
        cur = self._conn.execute(
            "SELECT * FROM research_tasks WHERE task_id = ?", (task_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        cols = [c[0] for c in cur.description]
        data = dict(zip(cols, row))
        return ResearchTask(
            task_id=data["task_id"],
            question_text=data["question_text"],
            created_by=data["created_by"],
            resolved_scope=json.loads(data["resolved_scope"]),
            state=TaskState(data["state"]),
            classification=Classification(data["classification"]),
            pending_clarification=data["pending_clarification"],
            claim_set_version=data["claim_set_version"],
            created_at=_parse_dt(data["created_at"]),
            updated_at=_parse_dt(data["updated_at"]),
            policy_id=data["policy_id"],
            policy_version=data["policy_version"],
        )

    def list_tasks(self) -> list[ResearchTask]:
        """Every persisted task, most recently created first. Phase 7
        addition - the read layer had no way to enumerate tasks before this
        (every prior caller already knew the task_id it wanted). Read-only,
        no new table, same row-to-dataclass mapping as get_task().
        """
        cur = self._conn.execute("SELECT * FROM research_tasks ORDER BY created_at DESC")
        cols = [c[0] for c in cur.description]
        tasks = []
        for row in cur.fetchall():
            data = dict(zip(cols, row))
            tasks.append(
                ResearchTask(
                    task_id=data["task_id"],
                    question_text=data["question_text"],
                    created_by=data["created_by"],
                    resolved_scope=json.loads(data["resolved_scope"]),
                    state=TaskState(data["state"]),
                    classification=Classification(data["classification"]),
                    pending_clarification=data["pending_clarification"],
                    claim_set_version=data["claim_set_version"],
                    created_at=_parse_dt(data["created_at"]),
                    updated_at=_parse_dt(data["updated_at"]),
                    policy_id=data["policy_id"],
                    policy_version=data["policy_version"],
                )
            )
        return tasks

    # -- Evidence ---------------------------------------------------------

    def save_evidence(self, evidence: Evidence) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO evidence
                (evidence_id, task_id, source_document_id, source_location,
                 raw_text, source_timestamp, retrieved_at, retrieval_tool,
                 content_hash, classification)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_id,
                evidence.task_id,
                evidence.source_document_id,
                evidence.source_location,
                evidence.raw_text,
                _dt(evidence.source_timestamp),
                _dt(evidence.retrieved_at),
                evidence.retrieval_tool,
                evidence.content_hash,
                evidence.classification.value,
            ),
        )
        self._conn.commit()

    def get_evidence(self, evidence_id: str) -> Evidence:
        cur = self._conn.execute(
            "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
        )
        row = cur.fetchone()
        cols = [c[0] for c in cur.description]
        data = dict(zip(cols, row))
        return Evidence(
            evidence_id=data["evidence_id"],
            task_id=data["task_id"],
            source_document_id=data["source_document_id"],
            source_location=data["source_location"],
            raw_text=data["raw_text"],
            source_timestamp=_parse_dt(data["source_timestamp"]),
            retrieved_at=_parse_dt(data["retrieved_at"]),
            retrieval_tool=data["retrieval_tool"],
            content_hash=data["content_hash"],
            classification=Classification(data["classification"]),
        )

    def list_evidence_for_task(self, task_id: str) -> list[Evidence]:
        cur = self._conn.execute(
            "SELECT evidence_id FROM evidence WHERE task_id = ?", (task_id,)
        )
        return [self.get_evidence(row[0]) for row in cur.fetchall()]

    # -- Claim --------------------------------------------------------------

    def save_claim(self, claim: Claim) -> None:
        self._conn.execute(
            """
            INSERT INTO claims
                (claim_id, task_id, claim_text, model_id, created_by,
                 support_status, support_strength, validator_result_id,
                 source_timestamp, reviewer_id, approval_status, subquestion,
                 claim_set_version, lifecycle_status, predecessor_claim_id,
                 successor_claim_id, withdrawn_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(claim_id) DO UPDATE SET
                support_status=excluded.support_status,
                support_strength=excluded.support_strength,
                validator_result_id=excluded.validator_result_id,
                reviewer_id=excluded.reviewer_id,
                approval_status=excluded.approval_status,
                lifecycle_status=excluded.lifecycle_status,
                predecessor_claim_id=excluded.predecessor_claim_id,
                successor_claim_id=excluded.successor_claim_id,
                withdrawn_reason=excluded.withdrawn_reason
            """,
            (
                claim.claim_id,
                claim.task_id,
                claim.claim_text,
                claim.model_id,
                claim.created_by,
                claim.support_status.value if claim.support_status else None,
                claim.support_strength,
                claim.validator_result_id,
                _dt(claim.source_timestamp),
                claim.reviewer_id,
                claim.approval_status.value,
                claim.subquestion,
                claim.claim_set_version,
                claim.lifecycle_status.value,
                claim.predecessor_claim_id,
                claim.successor_claim_id,
                claim.withdrawn_reason,
            ),
        )
        self._conn.commit()

    def get_claim(self, claim_id: str) -> Claim:
        cur = self._conn.execute(
            "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(claim_id)
        cols = [c[0] for c in cur.description]
        data = dict(zip(cols, row))
        return Claim(
            claim_id=data["claim_id"],
            task_id=data["task_id"],
            claim_text=data["claim_text"],
            model_id=data["model_id"],
            created_by=data["created_by"],
            support_status=SupportStatus(data["support_status"]) if data["support_status"] else None,
            support_strength=data["support_strength"],
            validator_result_id=data["validator_result_id"],
            source_timestamp=_parse_dt(data["source_timestamp"]),
            reviewer_id=data["reviewer_id"],
            approval_status=ApprovalStatus(data["approval_status"]),
            subquestion=data["subquestion"],
            claim_set_version=data["claim_set_version"],
            lifecycle_status=ClaimLifecycleStatus(data["lifecycle_status"]),
            predecessor_claim_id=data["predecessor_claim_id"],
            successor_claim_id=data["successor_claim_id"],
            withdrawn_reason=data["withdrawn_reason"],
        )

    def list_claims_for_task(self, task_id: str) -> list[Claim]:
        cur = self._conn.execute(
            "SELECT claim_id FROM claims WHERE task_id = ?", (task_id,)
        )
        return [self.get_claim(row[0]) for row in cur.fetchall()]

    # -- ClaimObjection (revision semantics) ---------------------------------

    def save_objection(self, objection: ClaimObjection) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO claim_objections
                (objection_id, task_id, claim_id, reviewer_id, reason,
                 claim_set_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                objection.objection_id,
                objection.task_id,
                objection.claim_id,
                objection.reviewer_id,
                objection.reason,
                objection.claim_set_version,
                _dt(objection.created_at),
            ),
        )
        self._conn.commit()

    def list_objections_for_claim(self, claim_id: str) -> list[ClaimObjection]:
        cur = self._conn.execute(
            "SELECT * FROM claim_objections WHERE claim_id = ? ORDER BY created_at", (claim_id,)
        )
        cols = [c[0] for c in cur.description]
        return [
            ClaimObjection(
                objection_id=data["objection_id"],
                task_id=data["task_id"],
                claim_id=data["claim_id"],
                reviewer_id=data["reviewer_id"],
                reason=data["reason"],
                claim_set_version=data["claim_set_version"],
                created_at=_parse_dt(data["created_at"]),
            )
            for data in (dict(zip(cols, r)) for r in cur.fetchall())
        ]

    def list_objections_for_task(self, task_id: str) -> list[ClaimObjection]:
        cur = self._conn.execute(
            "SELECT * FROM claim_objections WHERE task_id = ? ORDER BY created_at", (task_id,)
        )
        cols = [c[0] for c in cur.description]
        return [
            ClaimObjection(
                objection_id=data["objection_id"],
                task_id=data["task_id"],
                claim_id=data["claim_id"],
                reviewer_id=data["reviewer_id"],
                reason=data["reason"],
                claim_set_version=data["claim_set_version"],
                created_at=_parse_dt(data["created_at"]),
            )
            for data in (dict(zip(cols, r)) for r in cur.fetchall())
        ]

    # -- ClaimEvidenceLink (authoritative) -----------------------------------

    def save_link(self, link: ClaimEvidenceLink) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO claim_evidence_links
                (link_id, claim_id, evidence_id, quote_span,
                 support_contribution, relevance_score)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                link.link_id,
                link.claim_id,
                link.evidence_id,
                link.quote_span,
                link.support_contribution.value,
                link.relevance_score,
            ),
        )
        self._conn.commit()

    def list_links_for_claim(self, claim_id: str) -> list[ClaimEvidenceLink]:
        cur = self._conn.execute(
            "SELECT * FROM claim_evidence_links WHERE claim_id = ?", (claim_id,)
        )
        cols = [c[0] for c in cur.description]
        return [
            ClaimEvidenceLink(
                link_id=row["link_id"],
                claim_id=row["claim_id"],
                evidence_id=row["evidence_id"],
                quote_span=row["quote_span"],
                support_contribution=SupportContribution(row["support_contribution"]),
                relevance_score=row["relevance_score"],
            )
            for row in (dict(zip(cols, r)) for r in cur.fetchall())
        ]

    # -- ValidationResult (authoritative) ------------------------------------

    def record_validation_result(self, result: ValidationResult) -> None:
        """Persist a ValidationResult AND update the owning Claim's cached
        support_status/support_strength/validator_result_id in the same
        call, so the cache can never be set independently of the
        authoritative record it mirrors. See
        docs/CLAIM_EVIDENCE_MODEL.md#validation-authority.
        """
        self._conn.execute(
            """
            INSERT INTO validation_results
                (validation_id, claim_id, computed_support_status,
                 computed_support_strength, citation_check_passed,
                 conflicting_evidence_ids, missing_evidence_description,
                 validator_model_id, validated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.validation_id,
                result.claim_id,
                result.computed_support_status.value,
                result.computed_support_strength,
                int(result.citation_check_passed),
                json.dumps(result.conflicting_evidence_ids),
                result.missing_evidence_description,
                result.validator_model_id,
                _dt(result.validated_at),
            ),
        )
        claim = self.get_claim(result.claim_id)
        claim.support_status = result.computed_support_status
        claim.support_strength = result.computed_support_strength
        claim.validator_result_id = result.validation_id
        self.save_claim(claim)
        self._conn.commit()

    def list_validation_results_for_claim(self, claim_id: str) -> list[ValidationResult]:
        cur = self._conn.execute(
            "SELECT * FROM validation_results WHERE claim_id = ? ORDER BY validated_at",
            (claim_id,),
        )
        cols = [c[0] for c in cur.description]
        results = []
        for r in cur.fetchall():
            data = dict(zip(cols, r))
            results.append(
                ValidationResult(
                    validation_id=data["validation_id"],
                    claim_id=data["claim_id"],
                    computed_support_status=SupportStatus(data["computed_support_status"]),
                    computed_support_strength=data["computed_support_strength"],
                    citation_check_passed=bool(data["citation_check_passed"]),
                    conflicting_evidence_ids=json.loads(data["conflicting_evidence_ids"]),
                    missing_evidence_description=data["missing_evidence_description"],
                    validator_model_id=data["validator_model_id"],
                    validated_at=_parse_dt(data["validated_at"]),
                )
            )
        return results

    # -- Review ---------------------------------------------------------------

    def save_review(self, review: Review) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO reviews
                (review_id, task_id, reviewer_id, decision, comments,
                 claims_reviewed, claim_set_version, security_context, reviewed_at, policy_id, policy_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review.review_id,
                review.task_id,
                review.reviewer_id,
                review.decision.value,
                review.comments,
                json.dumps(review.claims_reviewed),
                review.claim_set_version,
                review.security_context,
                _dt(review.reviewed_at),
                review.policy_id,
                review.policy_version,
            ),
        )
        self._conn.commit()

    def list_reviews_for_task(self, task_id: str) -> list[Review]:
        cur = self._conn.execute(
            "SELECT * FROM reviews WHERE task_id = ? ORDER BY reviewed_at", (task_id,)
        )
        cols = [c[0] for c in cur.description]
        reviews = []
        for r in cur.fetchall():
            data = dict(zip(cols, r))
            reviews.append(
                Review(
                    review_id=data["review_id"],
                    task_id=data["task_id"],
                    reviewer_id=data["reviewer_id"],
                    decision=ReviewDecision(data["decision"]),
                    comments=data["comments"],
                    claims_reviewed=json.loads(data["claims_reviewed"]),
                    claim_set_version=data["claim_set_version"],
                    security_context=data["security_context"],
                    reviewed_at=_parse_dt(data["reviewed_at"]),
                    policy_id=data["policy_id"],
                    policy_version=data["policy_version"],
                )
            )
        return reviews

    # -- ModelCall (audit trace) -----------------------------------------------

    def save_model_call(self, call: ModelCall) -> None:
        self._conn.execute(
            """
            INSERT INTO model_calls
                (model_call_id, task_id, purpose, provider_id, model_id,
                 routing_classification, input_summary, output_summary,
                 latency_ms, tokens_in, tokens_out, cost, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call.model_call_id,
                call.task_id,
                call.purpose,
                call.provider_id,
                call.model_id,
                call.routing_classification.value,
                call.input_summary,
                call.output_summary,
                call.latency_ms,
                call.tokens_in,
                call.tokens_out,
                call.cost,
                _dt(call.created_at),
            ),
        )
        self._conn.commit()

    def list_model_calls_for_task(self, task_id: str) -> list[ModelCall]:
        cur = self._conn.execute(
            "SELECT * FROM model_calls WHERE task_id = ? ORDER BY created_at", (task_id,)
        )
        cols = [c[0] for c in cur.description]
        calls = []
        for r in cur.fetchall():
            data = dict(zip(cols, r))
            calls.append(
                ModelCall(
                    model_call_id=data["model_call_id"],
                    task_id=data["task_id"],
                    purpose=data["purpose"],
                    provider_id=data["provider_id"],
                    model_id=data["model_id"],
                    routing_classification=Classification(data["routing_classification"]),
                    input_summary=data["input_summary"],
                    output_summary=data["output_summary"],
                    latency_ms=data["latency_ms"],
                    tokens_in=data["tokens_in"],
                    tokens_out=data["tokens_out"],
                    cost=data["cost"],
                    created_at=_parse_dt(data["created_at"]),
                )
            )
        return calls

    # -- ToolCall (audit trace) ------------------------------------------------

    def save_tool_call(self, call: ToolCall) -> None:
        self._conn.execute(
            """
            INSERT INTO tool_calls
                (tool_call_id, task_id, tool_name, input_summary,
                 output_summary, succeeded, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call.tool_call_id,
                call.task_id,
                call.tool_name,
                call.input_summary,
                call.output_summary,
                int(call.succeeded),
                _dt(call.created_at),
            ),
        )
        self._conn.commit()

    def list_tool_calls_for_task(self, task_id: str) -> list[ToolCall]:
        cur = self._conn.execute(
            "SELECT * FROM tool_calls WHERE task_id = ? ORDER BY created_at", (task_id,)
        )
        cols = [c[0] for c in cur.description]
        calls = []
        for r in cur.fetchall():
            data = dict(zip(cols, r))
            calls.append(
                ToolCall(
                    tool_call_id=data["tool_call_id"],
                    task_id=data["task_id"],
                    tool_name=data["tool_name"],
                    input_summary=data["input_summary"],
                    output_summary=data["output_summary"],
                    succeeded=bool(data["succeeded"]),
                    created_at=_parse_dt(data["created_at"]),
                )
            )
        return calls

    # -- StateTransition (audit trace) ------------------------------------------

    def save_state_transition(self, transition: StateTransition) -> None:
        self._conn.execute(
            """
            INSERT INTO state_transitions
                (transition_id, task_id, from_state, to_state, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                transition.transition_id,
                transition.task_id,
                transition.from_state.value,
                transition.to_state.value,
                _dt(transition.occurred_at),
            ),
        )
        self._conn.commit()

    def list_state_transitions_for_task(self, task_id: str) -> list[StateTransition]:
        cur = self._conn.execute(
            "SELECT * FROM state_transitions WHERE task_id = ? ORDER BY occurred_at", (task_id,)
        )
        cols = [c[0] for c in cur.description]
        transitions = []
        for r in cur.fetchall():
            data = dict(zip(cols, r))
            transitions.append(
                StateTransition(
                    transition_id=data["transition_id"],
                    task_id=data["task_id"],
                    from_state=TaskState(data["from_state"]),
                    to_state=TaskState(data["to_state"]),
                    occurred_at=_parse_dt(data["occurred_at"]),
                )
            )
        return transitions

    # -- SecurityEvent (audit trace, Phase 4) -----------------------------------

    def save_security_event(self, event: SecurityEvent) -> None:
        self._conn.execute(
            """
            INSERT INTO security_events
                (security_event_id, task_id, trigger, classification, action_taken,
                 detected_categories, requested_provider_id, selected_provider_id,
                 policy_result, resolved_by, created_at, policy_id, policy_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.security_event_id,
                event.task_id,
                event.trigger,
                event.classification.value,
                event.action_taken.value,
                json.dumps(event.detected_categories),
                event.requested_provider_id,
                event.selected_provider_id,
                event.policy_result,
                event.resolved_by,
                _dt(event.created_at),
                event.policy_id,
                event.policy_version,
            ),
        )
        self._conn.commit()

    def get_security_event(self, security_event_id: str) -> SecurityEvent:
        cur = self._conn.execute(
            "SELECT * FROM security_events WHERE security_event_id = ?", (security_event_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(security_event_id)
        cols = [c[0] for c in cur.description]
        data = dict(zip(cols, row))
        return SecurityEvent(
            security_event_id=data["security_event_id"],
            task_id=data["task_id"],
            trigger=data["trigger"],
            classification=Classification(data["classification"]),
            action_taken=DLPAction(data["action_taken"]),
            detected_categories=json.loads(data["detected_categories"]),
            requested_provider_id=data["requested_provider_id"],
            selected_provider_id=data["selected_provider_id"],
            policy_result=data["policy_result"],
            policy_id=data["policy_id"],
            policy_version=data["policy_version"],
            resolved_by=data["resolved_by"],
            created_at=_parse_dt(data["created_at"]),
        )

    def resolve_security_event(self, security_event_id: str, resolved_by: str) -> None:
        """Sets resolved_by on a persisted SecurityEvent - Phase 5's real
        implementation of the field docs/CLAIM_EVIDENCE_MODEL.md#securityevent
        already documented in Phase 0 ("Set for require_approval events once
        a human acts"). Does not touch ResearchTask.state - see
        afra.orchestrator.orchestrator.resolve_security_event for why this
        is deliberately not a task-state override.
        """
        cur = self._conn.execute(
            "UPDATE security_events SET resolved_by = ? WHERE security_event_id = ?",
            (resolved_by, security_event_id),
        )
        if cur.rowcount == 0:
            raise KeyError(security_event_id)
        self._conn.commit()

    def list_security_events_for_task(self, task_id: str) -> list[SecurityEvent]:
        cur = self._conn.execute(
            "SELECT * FROM security_events WHERE task_id = ? ORDER BY created_at", (task_id,)
        )
        cols = [c[0] for c in cur.description]
        events = []
        for r in cur.fetchall():
            data = dict(zip(cols, r))
            events.append(
                SecurityEvent(
                    security_event_id=data["security_event_id"],
                    task_id=data["task_id"],
                    trigger=data["trigger"],
                    classification=Classification(data["classification"]),
                    action_taken=DLPAction(data["action_taken"]),
                    detected_categories=json.loads(data["detected_categories"]),
                    requested_provider_id=data["requested_provider_id"],
                    selected_provider_id=data["selected_provider_id"],
                    policy_result=data["policy_result"],
                    policy_id=data["policy_id"],
                    policy_version=data["policy_version"],
                    resolved_by=data["resolved_by"],
                    created_at=_parse_dt(data["created_at"]),
                )
            )
        return events
