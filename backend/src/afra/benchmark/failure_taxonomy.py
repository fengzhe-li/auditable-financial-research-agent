"""A deterministic outcome/failure taxonomy for benchmark task attempts -
see docs/BENCHMARK_ANALYSIS.md#failure-taxonomy.

Read-only and additive: classify_task_outcome() takes exactly the JSON
shape already persisted, unchanged, by afra.benchmark.runner (Phase 6
deterministic) and afra.benchmark.real_model_harness (Phase 6.5 live) -
`{"task": {...}, "result": {...}, "score": {...}, ...}` for a graded
attempt, or `{"task": {...}, "status": "incomplete"|"rate_limited"|
"provider_error", ...}` for one that never reached grading. Nothing here
imports afra.providers.real_model or afra.benchmark.real_model_harness, and
nothing here writes anything - it only interprets task-result dicts a
caller already loaded from disk (or already holds in memory).

The point of a shared taxonomy across both harnesses: a live run's
"provider_error"/"rate_limited"/"incomplete" statuses and a deterministic
run's ordinary graded outcomes are different *kinds* of thing (one is an
attempt that never reached grading at all; the other did), and conflating
them - e.g. quietly treating a rate-limited attempt as if the model had
answered and gotten it wrong - would corrupt exactly the model-quality
metrics this benchmark exists to measure. See
docs/BENCHMARK_ANALYSIS.md#infrastructure-vs-model-quality-separation.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum


class OutcomeCategory(str, Enum):
    """One canonical label per task attempt. INFRASTRUCTURE_CATEGORIES below
    is the subset that means "never reached grading at all" - the rest are
    all attempts the deterministic grader (afra.benchmark.grader, unmodified)
    actually scored, including the three "abstained/blocked/clarified"
    categories that are correct, gradable, *non-failure* terminal states,
    not something a model got wrong.
    """

    # -- Provider/operational: never reached grading. Excluded from every
    # model-quality denominator - see docs/BENCHMARK_ANALYSIS.md.
    INCOMPLETE = "incomplete"  # non-STOP finish_reason, not specifically MAX_TOKENS (e.g. SAFETY)
    MAX_TOKENS = "max_tokens"  # the specific, expected-common incomplete case
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    MALFORMED_OUTPUT = "malformed_output"  # graded, but the persisted shape itself is unreadable/inconsistent

    # -- Gradable, substantive terminal outcomes that are not a plain
    # "answered" completion - correct behaviour for these categories is
    # often reaching exactly this state, not avoiding it.
    POLICY_BLOCKED = "policy_blocked"
    CLARIFICATION_REQUIRED = "clarification_required"
    ABSTENTION = "abstention"  # INSUFFICIENT_EVIDENCE - insufficient_evidence/conflicting_evidence tasks

    # -- Gradable, ordinary completion.
    COMPLETED = "completed"

    # -- Recognized-but-uncategorizable (e.g. graded with completed=False
    # and none of the above states matched) or genuinely unrecognized shape.
    OTHER = "other"


# Attempts that never reached the deterministic grader at all - see this
# module's docstring. Every other category was scored by
# afra.benchmark.grader.grade_task() (unmodified).
INFRASTRUCTURE_CATEGORIES = frozenset(
    {
        OutcomeCategory.INCOMPLETE,
        OutcomeCategory.MAX_TOKENS,
        OutcomeCategory.RATE_LIMITED,
        OutcomeCategory.PROVIDER_ERROR,
        OutcomeCategory.MALFORMED_OUTPUT,
    }
)


def is_infrastructure_failure(category: OutcomeCategory) -> bool:
    return category in INFRASTRUCTURE_CATEGORIES


_STATUS_TO_CATEGORY = {
    "rate_limited": OutcomeCategory.RATE_LIMITED,
    "provider_error": OutcomeCategory.PROVIDER_ERROR,
}

_FINAL_STATE_TO_CATEGORY = {
    "SECURITY_BLOCKED": OutcomeCategory.POLICY_BLOCKED,
    "NEEDS_CLARIFICATION": OutcomeCategory.CLARIFICATION_REQUIRED,
    "INSUFFICIENT_EVIDENCE": OutcomeCategory.ABSTENTION,
}


def classify_task_outcome(task_json: dict) -> OutcomeCategory:
    """Classifies one persisted task-result dict. Never raises on an
    unrecognized shape - falls back to OTHER, since this is a read-only
    analysis layer over artifacts it did not produce and must not crash a
    report over one malformed file.
    """
    if not isinstance(task_json, dict):
        return OutcomeCategory.OTHER

    status = task_json.get("status")
    if status == "incomplete":
        finish_reason = task_json.get("finish_reason")
        return OutcomeCategory.MAX_TOKENS if finish_reason == "MAX_TOKENS" else OutcomeCategory.INCOMPLETE
    if status in _STATUS_TO_CATEGORY:
        return _STATUS_TO_CATEGORY[status]
    if status is not None and status not in ("incomplete",):
        # A recognized "did not reach grading" marker this taxonomy doesn't
        # have a specific bucket for yet - still an infrastructure/
        # operational outcome, not a model-quality one.
        return OutcomeCategory.PROVIDER_ERROR

    score = task_json.get("score")
    if not isinstance(score, dict):
        # No "status" (not an infrastructure failure) and no "score"
        # either - not a shape this taxonomy recognizes at all.
        return OutcomeCategory.OTHER

    result = task_json.get("result")
    final_state = result.get("final_state") if isinstance(result, dict) else None
    if final_state in _FINAL_STATE_TO_CATEGORY:
        return _FINAL_STATE_TO_CATEGORY[final_state]

    completed = score.get("completed")
    if completed is True:
        return OutcomeCategory.COMPLETED
    if completed is False:
        return OutcomeCategory.OTHER
    # "score" exists but has no readable "completed" field - the persisted
    # shape is inconsistent with what afra.benchmark.grader.TaskScore
    # actually writes.
    return OutcomeCategory.MALFORMED_OUTPUT


@dataclass
class OutcomeSummary:
    """Deterministic counts over a set of persisted task-result dicts -
    typically every *.json file under one run/configuration/repeat's
    tasks/ directory. Always reports explicit denominators (this is the
    point - see docs/BENCHMARK_ANALYSIS.md#denominators): `total` is every
    task attempt considered, `infrastructure_failures` is the subset that
    never reached grading, `gradable` is what's left for model-quality
    metrics to be computed over (afra.benchmark.aggregate.aggregate()
    already only ever sees this gradable subset, for any harness that
    excludes infrastructure failures from `scores` before aggregating -
    this class exists to make that exclusion visible and countable, not to
    change it).
    """

    total: int
    by_category: dict[str, int] = field(default_factory=dict)
    infrastructure_failures: int = 0
    gradable: int = 0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "by_category": dict(self.by_category),
            "infrastructure_failures": self.infrastructure_failures,
            "gradable": self.gradable,
            "infrastructure_failure_rate": (self.infrastructure_failures / self.total) if self.total else None,
        }


def summarize_outcomes(task_jsons: list[dict]) -> OutcomeSummary:
    counts: Counter[str] = Counter()
    infra = 0
    for task_json in task_jsons:
        category = classify_task_outcome(task_json)
        counts[category.value] += 1
        if is_infrastructure_failure(category):
            infra += 1
    total = len(task_jsons)
    return OutcomeSummary(
        total=total,
        by_category=dict(counts),
        infrastructure_failures=infra,
        gradable=total - infra,
    )
