"""The benchmark task schema from docs/EVALUATION_PLAN.md#task-schema,
implemented for real in Phase 6.

A BenchmarkTask is pure data - gold-labelled by hand (by the person writing
this benchmark, not by any model), never by running the system under test
and copying its output back as "gold". See docs/PHASE_6_REPORT.md's grader
design section for why this matters: a benchmark whose gold labels came from
the system it evaluates would validate nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# The 8 categories from the Phase 6 spec (docs/ROADMAP.md's Phase 6 entry
# records this as a deliberate narrowing from docs/EVALUATION_PLAN.md's
# original 9-category list, which also included "evidence-supported causal
# interpretation" - not carried into this benchmark version).
CATEGORIES = frozenset(
    {
        "factual_retrieval",
        "multi_document_comparison",
        "temporal_change_analysis",
        "ambiguous_clarification",
        "insufficient_evidence",
        "conflicting_evidence",
        "prompt_injection",
        "sensitive_data_routing_policy",
    }
)

# Configuration D's task scope excludes this one category - see
# docs/PHASE_6_REPORT.md's "does the benchmark actually distinguish
# configurations" section for the architectural reason (Phase 4 policy
# enforcement is unconditionally wired into the shared claim-drafting path
# and cannot be disabled without weakening a real invariant, which Phase 6
# was explicitly told not to do).
CONFIGURATION_D_EXCLUDED_CATEGORIES = frozenset({"sensitive_data_routing_policy"})


@dataclass(frozen=True)
class GoldEvidence:
    document_id: str
    quote_span: str

    def to_dict(self) -> dict:
        return {"document_id": self.document_id, "quote_span": self.quote_span}

    @staticmethod
    def from_dict(data: dict) -> "GoldEvidence":
        return GoldEvidence(document_id=data["document_id"], quote_span=data["quote_span"])


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    question: str
    category: str
    required_documents: list[str] = field(default_factory=list)
    gold_evidence: list[GoldEvidence] = field(default_factory=list)
    answerable: bool = True
    requires_clarification: bool = False
    expected_claims: list[str] = field(default_factory=list)
    expected_abstention_reason: str | None = None
    security_classification: str | None = None  # PUBLIC/INTERNAL/CONFIDENTIAL/RESTRICTED, or None
    expected_policy_outcome: str | None = None  # allow/block/route_private/require_approval, or None
    # Harness-level simulation, applied identically across all three
    # configurations: these fixture document_ids are excluded from whatever
    # each configuration would otherwise see (retrieved evidence for D/F;
    # the concatenated corpus for A) - see
    # afra.benchmark.configurations.run_configuration_d's docstring. Models
    # "this filing was missing/unavailable", the deterministic mechanism
    # behind this benchmark's insufficient_evidence-category tasks and the
    # "missing filing" failure-injection case (docs/ROADMAP.md's list) -
    # not a claim that real retrieval actually failed.
    simulate_missing_documents: list[str] = field(default_factory=list)
    # conflicting_evidence category only: a manually-specified claim +
    # evidence links (document_id, quote_span, contribution), bypassing the
    # normal one-document-per-company autonomous routing. Necessary because
    # afra.routing.router.pick_latest_document only ever retrieves the
    # latest filing per company by design (a disclosed Phase 3 limitation -
    # see docs/PHASE_3_REPORT.md), so a genuine same-claim, cross-year
    # contradiction cannot arise from unmodified autonomous routing alone.
    # Mirrors the exact manual construction already established and tested
    # in tests/test_phase3_end_to_end.py's
    # test_conflicting_evidence_remains_unresolved_via_autonomous_flow -
    # not a new mechanism invented for the benchmark.
    manual_conflicting_claim: dict | None = None
    # sensitive_data_routing_policy category (mostly): retrieves exactly
    # this document instead of letting afra.routing.router.pick_latest_document
    # pick whichever Contoso/Fabrikam document has the latest published_at
    # date - which, for this fixture corpus, is always one of the ordinary
    # 10-Ks, never the INTERNAL/CONFIDENTIAL/RESTRICTED documents (they are
    # all dated earlier than the same company's latest 10-K - see
    # afra/tools/fixtures.py). Represents "the analyst explicitly retrieved
    # this specific document", not a claim that autonomous routing can find
    # it on its own - see docs/PHASE_6_REPORT.md's methodology section for
    # this being a real, disclosed router limitation the benchmark itself
    # surfaced, not something quietly worked around.
    manual_document_override: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown benchmark category: {self.category!r}")

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "question": self.question,
            "category": self.category,
            "required_documents": self.required_documents,
            "gold_evidence": [g.to_dict() for g in self.gold_evidence],
            "answerable": self.answerable,
            "requires_clarification": self.requires_clarification,
            "expected_claims": self.expected_claims,
            "expected_abstention_reason": self.expected_abstention_reason,
            "security_classification": self.security_classification,
            "expected_policy_outcome": self.expected_policy_outcome,
            "simulate_missing_documents": self.simulate_missing_documents,
            "manual_conflicting_claim": self.manual_conflicting_claim,
            "manual_document_override": self.manual_document_override,
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(data: dict) -> "BenchmarkTask":
        return BenchmarkTask(
            task_id=data["task_id"],
            question=data["question"],
            category=data["category"],
            required_documents=data.get("required_documents", []),
            gold_evidence=[GoldEvidence.from_dict(g) for g in data.get("gold_evidence", [])],
            answerable=data.get("answerable", True),
            requires_clarification=data.get("requires_clarification", False),
            expected_claims=data.get("expected_claims", []),
            expected_abstention_reason=data.get("expected_abstention_reason"),
            security_classification=data.get("security_classification"),
            expected_policy_outcome=data.get("expected_policy_outcome"),
            simulate_missing_documents=data.get("simulate_missing_documents", []),
            manual_conflicting_claim=data.get("manual_conflicting_claim"),
            manual_document_override=data.get("manual_document_override"),
            notes=data.get("notes", ""),
        )


@dataclass(frozen=True)
class BenchmarkSuite:
    version: str
    tasks: list[BenchmarkTask]

    def to_dict(self) -> dict:
        return {"version": self.version, "tasks": [t.to_dict() for t in self.tasks]}

    @staticmethod
    def from_dict(data: dict) -> "BenchmarkSuite":
        return BenchmarkSuite(
            version=data["version"], tasks=[BenchmarkTask.from_dict(t) for t in data["tasks"]]
        )

    def by_category(self, category: str) -> list[BenchmarkTask]:
        return [t for t in self.tasks if t.category == category]


def save_benchmark(suite: BenchmarkSuite, path: str | Path) -> None:
    Path(path).write_text(json.dumps(suite.to_dict(), indent=2) + "\n")


def load_benchmark(path: str | Path) -> BenchmarkSuite:
    return BenchmarkSuite.from_dict(json.loads(Path(path).read_text()))
