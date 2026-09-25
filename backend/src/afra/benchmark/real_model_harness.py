"""Phase 6.5 real-model harness - see docs/PHASE_6_5_REAL_MODEL_REPORT.md.

Builds on the existing Phase 6 deterministic harness (afra.benchmark.
configurations/grader/aggregate), unchanged - this module adds exactly what
Phase 6.5 needs on top:

- A frozen, smaller task subset (backend/benchmark/real_model_subset_v1.json,
  docs/PHASE_6_REPORT.md#13's proposed factual_retrieval + conflicting_evidence
  slice - 13 tasks, generated from tasks_v1.json by category filter, not a
  hand-maintained list that could silently drift).
- dry_run(): validates everything a live run would need with zero external
  calls and no credentials required.
- live_run(): actually substitutes a real provider and calls the model -
  never invoked by any test or by CI.
- Repeat-count support, since a real model is not deterministic call-to-call
  the way every provider through Phase 6 was.
- A run manifest carrying reproducibility metadata beyond Phase 6's
  (harness_version, git_commit, python_version, credentials_present, mode).
- Per-task handling of the three ways a real-provider call can fail without
  crashing the whole run (afra.providers.real_model): a non-STOP
  finish_reason (IncompleteCompletionError), exhausted 429 retries
  (RateLimitExhaustedError), or any other provider error - each recorded
  as its own never-graded task result and rolled into the manifest's
  incomplete_attempts/rate_limited_attempts/provider_error_attempts counts.

Configuration scope is still only A/D/F - Phase 6.5 does not revisit the
B/C/E decision recorded in docs/PHASE_6_REPORT.md.
"""

from __future__ import annotations

import json
import platform
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from afra.benchmark.aggregate import aggregate
from afra.benchmark.baseline_provider import SingleShotBaselineProvider
from afra.benchmark.configurations import (
    TaskRunResult,
    build_full_provider_registry,
    run_configuration_a,
    run_configuration_d,
    run_configuration_f,
)
from afra.benchmark.grader import TaskScore, grade_task
from afra.benchmark.schema import BenchmarkSuite, BenchmarkTask
from afra.domain.enums import ProviderClass
from afra.providers.base import ModelProvider
from afra.providers.real_model import (
    AntigravityClaimDraftProvider,
    GeminiClaimDraftProvider,
    HybridDraftClaimProvider,
    build_real_provider,
)
from afra.storage.repository import Repository

SUPPORTED_CONFIGURATIONS = ("A", "D", "F")
HARNESS_VERSION = "phase_6_5_real_model_harness_v1"

# Corrected selection (see docs/ROADMAP.md's Phase 6.5 deviations entry):
# docs/PHASE_6_REPORT.md#13 proposed factual_retrieval + conflicting_evidence
# only, on the grounds that prompt_injection/sensitive_data_routing_policy
# test architectural/policy properties independent of which model drafts
# claim text. That reasoning is still correct for *those two* categories,
# but it does not extend to multi_document_comparison, ambiguous_clarification,
# or insufficient_evidence, which §13 never actually excluded - the first
# real_model_subset_v1.json dropped them by mistake. The frozen subset must
# cover every category this benchmark's metrics (docs/EVALUATION_PLAN.md) are
# defined over: unsupported-claim rate and citation precision need answerable
# categories beyond factual_retrieval; abstention accuracy needs
# insufficient_evidence and conflicting_evidence; clarification accuracy
# needs ambiguous_clarification; injection-success rate needs prompt_injection;
# policy-block accuracy needs sensitive_data_routing_policy.
# temporal_change_analysis is the one Phase 6 category deliberately left out
# - docs/PHASE_6_REPORT.md's §6 already established it always produces a
# mechanical 0.5 retrieval_recall regardless of provider (a router
# limitation, not something a real model's claim-drafting text can affect),
# so re-running it here would not produce new information either.
REQUIRED_CATEGORIES = frozenset(
    {
        "factual_retrieval",
        "multi_document_comparison",
        "ambiguous_clarification",
        "insufficient_evidence",
        "conflicting_evidence",
        "prompt_injection",
        "sensitive_data_routing_policy",
    }
)

# The deterministic, documented selection rule - fixed here, before any
# live-model execution, per this phase's explicit instruction.
#
# Default: the first TASKS_PER_CATEGORY (2) tasks per category, sorted by
# task_id. 7 categories x 2 = 14 tasks - within the required 12-18 range,
# exactly stratified (no category overweighted), and the smallest even
# split that still puts more than one task behind every metric.
TASKS_PER_CATEGORY = 2

# Three categories carry a gold-field dimension a plain "first 2 by
# task_id" pick would collapse to one value - overridden here to the
# smallest pair that covers both. Every other required category is
# homogeneous across the fields this benchmark's grader actually reads (see
# docs/PHASE_6_REPORT.md's task-distribution table), so "first 2 by
# task_id" already gives meaningful coverage there.
_CATEGORY_OVERRIDES: dict[str, tuple[str, ...]] = {
    # AC-01..03/06's notes read "comparison requested, no axis named";
    # AC-04/05's read "companies outside this system's known corpus" - two
    # structurally different reasons NEEDS_CLARIFICATION is reached, so
    # both are represented rather than two instances of the same one.
    "ambiguous_clarification": ("AC-01", "AC-04"),
    # IE-01..03 simulate FABRIKAM-2025-10K missing, IE-04..06 simulate
    # CONTOSO-2025-10K missing - both companies' one-sided-comparison case
    # represented, not just one.
    "insufficient_evidence": ("IE-01", "IE-04"),
    # SD-01/02/05 have expected_policy_outcome="allow", SD-03/04 have
    # "block" - policy-block accuracy is only a meaningful signal if both
    # outcomes are actually exercised, not just the allow path.
    "sensitive_data_routing_policy": ("SD-01", "SD-03"),
}

_BACKEND_DIR = Path(__file__).resolve().parents[3]
SUBSET_PATH = _BACKEND_DIR / "benchmark" / "real_model_subset_v1.json"


class InvalidConfigurationError(ValueError):
    pass


class InvalidRepeatCountError(ValueError):
    pass


@dataclass(frozen=True)
class RealModelSubset:
    version: str
    source_benchmark_version: str
    categories: list[str]
    task_ids: list[str]
    rationale: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "source_benchmark_version": self.source_benchmark_version,
            "categories": self.categories,
            "task_ids": self.task_ids,
            "rationale": self.rationale,
        }

    @staticmethod
    def from_dict(data: dict) -> "RealModelSubset":
        return RealModelSubset(
            version=data["version"],
            source_benchmark_version=data["source_benchmark_version"],
            categories=data["categories"],
            task_ids=data["task_ids"],
            rationale=data["rationale"],
        )


def derive_subset_task_ids(suite: BenchmarkSuite) -> list[str]:
    """The provenance for real_model_subset_v1.json's task_ids: the
    deterministic, documented stratified-selection rule above
    (TASKS_PER_CATEGORY / _CATEGORY_OVERRIDES), computed the same way both
    by backend/scripts/build_real_model_subset_v1.py (which writes the file)
    and by tests (which check the checked-in file matches this derivation) -
    see docs/PHASE_6_REPORT.md's benchmark-provenance discipline, extended
    here. Task order within the result is grouped by category (sorted), then
    by task_id within a category - stable and reproducible, not suite
    iteration order.
    """
    by_category: dict[str, list[BenchmarkTask]] = {}
    for task in suite.tasks:
        if task.category in REQUIRED_CATEGORIES:
            by_category.setdefault(task.category, []).append(task)

    task_ids: list[str] = []
    for category in sorted(REQUIRED_CATEGORIES):
        candidates = sorted(by_category.get(category, []), key=lambda t: t.task_id)
        if category in _CATEGORY_OVERRIDES:
            picked = list(_CATEGORY_OVERRIDES[category])
            available = {t.task_id for t in candidates}
            missing = [task_id for task_id in picked if task_id not in available]
            if missing:
                raise ValueError(f"{category}: override task_id(s) {missing} not found in suite")
        else:
            picked = [t.task_id for t in candidates[:TASKS_PER_CATEGORY]]
        task_ids.extend(picked)
    return task_ids


def save_subset(subset: RealModelSubset, path: str | Path = SUBSET_PATH) -> None:
    Path(path).write_text(json.dumps(subset.to_dict(), indent=2) + "\n")


def load_subset(path: str | Path = SUBSET_PATH) -> RealModelSubset:
    return RealModelSubset.from_dict(json.loads(Path(path).read_text()))


def subset_tasks(suite: BenchmarkSuite, subset: RealModelSubset) -> list[BenchmarkTask]:
    by_id = {t.task_id: t for t in suite.tasks}
    return [by_id[task_id] for task_id in subset.task_ids]  # KeyError if a subset id doesn't exist in suite


def _validate_configurations(configurations: tuple[str, ...]) -> None:
    unknown = sorted(set(configurations) - set(SUPPORTED_CONFIGURATIONS))
    if unknown:
        raise InvalidConfigurationError(
            f"unsupported configuration(s) {unknown} - only {SUPPORTED_CONFIGURATIONS} exist "
            "(docs/PHASE_6_REPORT.md's B/C/E decision, unrevisited in Phase 6.5)"
        )
    if not configurations:
        raise InvalidConfigurationError("at least one configuration must be given")


def _validate_repeat_count(repeat_count: int) -> None:
    if repeat_count < 1:
        raise InvalidRepeatCountError(f"repeat_count must be >= 1, got {repeat_count}")


def _git_commit() -> str | None:
    """Best-effort only - this repository may not be under git at all (or
    may be at any given moment); reproducibility metadata degrades to None
    rather than failing the whole manifest."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _manifest(
    *,
    mode: str,
    run_id: str,
    suite: BenchmarkSuite,
    subset: RealModelSubset,
    configurations: tuple[str, ...],
    repeat_count: int,
    provider_id: str,
    model_id: str,
    credentials_present: bool,
    notes: str,
    incomplete_attempts: int = 0,
    rate_limited_attempts: int = 0,
    provider_error_attempts: int = 0,
) -> dict:
    return {
        "run_id": run_id,
        "harness_version": HARNESS_VERSION,
        "mode": mode,  # "dry_run" | "live"
        "benchmark_version": suite.version,
        "subset_version": subset.version,
        "subset_categories": list(subset.categories),
        "subset_task_ids": list(subset.task_ids),
        "configurations": list(configurations),
        "repeat_count": repeat_count,
        "provider_id": provider_id,
        "model_id": model_id,
        "credentials_present": credentials_present,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python_version": platform.python_version(),
        "git_commit": _git_commit(),
        "notes": notes,
        # Count of task attempts where the real provider reported a
        # non-STOP finish_reason (e.g. MAX_TOKENS) and were therefore
        # excluded from grading entirely - see
        # afra.providers.real_model.IncompleteCompletionError. Always 0 for
        # mode="dry_run" (no calls are ever made).
        "incomplete_attempts": incomplete_attempts,
        # Count of task attempts where every retry allowed by
        # GeminiClaimDraftProvider.max_retries still hit 429
        # RESOURCE_EXHAUSTED - see afra.providers.real_model
        # .RateLimitExhaustedError. Always 0 for mode="dry_run".
        "rate_limited_attempts": rate_limited_attempts,
        # Count of task attempts that failed with any other (non-429,
        # non-incomplete) real-provider error - network failure, a
        # different APIError, etc. - caught at the task level so one bad
        # attempt never crashes the whole live run. Always 0 for
        # mode="dry_run".
        "provider_error_attempts": provider_error_attempts,
    }


@dataclass
class DryRunReport:
    manifest: dict
    task_count: int
    tasks_validated: list[str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "manifest": self.manifest,
            "task_count": self.task_count,
            "tasks_validated": self.tasks_validated,
            "warnings": self.warnings,
        }


def dry_run(
    *,
    suite: BenchmarkSuite,
    subset: RealModelSubset,
    configurations: tuple[str, ...] = ("F",),
    repeat_count: int = 1,
    provider: ModelProvider | None = None,
) -> DryRunReport:
    """Validates everything a live run would need - subset tasks resolve
    against the suite, configurations are in {"A","D","F"}, repeat_count is
    sane, and whether credentials are present in the environment - without
    ever calling provider.complete() or importing the `google-genai` package.
    Zero external calls, by construction: nothing below this docstring calls
    anything on `provider` except its static declared attributes and
    credentials_present() (an os.environ read, not a network call).
    """
    _validate_configurations(configurations)
    _validate_repeat_count(repeat_count)
    provider = provider or GeminiClaimDraftProvider()

    tasks = subset_tasks(suite, subset)
    warnings = []
    if not provider.credentials_present():
        if hasattr(provider, "api_key_env") and getattr(provider, "api_key_env"):
            warnings.append(
                f"${provider.api_key_env} is not set - a live run would fail fast in "
                f"{type(provider).__name__}.complete() before any network call, "
                "raising RealModelCredentialsMissingError."
            )
        else:
            warnings.append(
                f"{type(provider).__name__} is unavailable ('agy' CLI binary missing or unauthenticated) - "
                f"a live run would fail fast in {type(provider).__name__}.complete() before any model invocation, "
                "raising RealModelCredentialsMissingError."
            )

    manifest = _manifest(
        mode="dry_run",
        run_id=f"realmodel_dryrun_{uuid.uuid4().hex[:12]}",
        suite=suite,
        subset=subset,
        configurations=configurations,
        repeat_count=repeat_count,
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        credentials_present=provider.credentials_present(),
        notes=(
            "No real external LLM has been called. This is a validation-only "
            "run - see docs/PHASE_6_5_REAL_MODEL_REPORT.md."
        ),
    )
    return DryRunReport(manifest=manifest, task_count=len(tasks), tasks_validated=[t.task_id for t in tasks], warnings=warnings)


def _provider_observability(hybrid: HybridDraftClaimProvider) -> dict:
    """Reads whatever observability fields the real provider currently
    behind `hybrid` exposes - finish_reason (this call's own outcome, not
    just the last successful one - see GeminiClaimDraftProvider
    .last_call_finish_reason), latency, token usage (including
    thoughts_token_count where the SDK exposes it), output text length,
    retry_count, rate_limit_events, and a final provider status - always
    through getattr(..., None/default), never a hard attribute access, so a
    provider that doesn't set them (any deterministic test double used in
    tests standing in for the real adapter) never breaks the harness.

    Caller's responsibility, not this function's: only call this when the
    task currently being written was actually served by `hybrid
    .real_provider` (see _run_repeat()'s call_count-diff check) - this
    function has no way to know which provider served a given task, and
    will happily read `hybrid.real_provider`'s last (possibly stale, from a
    *different* task) state regardless. See afra.providers.real_model
    .GeminiClaimDraftProvider's last_* attributes.
    """
    provider = hybrid.real_provider
    return {
        "finish_reason": getattr(provider, "last_call_finish_reason", None),
        "latency_ms": getattr(provider, "last_latency_ms", None),
        "token_usage": getattr(provider, "last_token_usage", None),
        "output_text_length": getattr(provider, "last_output_text_length", None),
        "retry_count": getattr(provider, "last_retry_count", None),
        "rate_limit_events": getattr(provider, "last_rate_limit_events", None),
        "provider_status": getattr(provider, "last_provider_status", None),
    }


def _write_task_result(
    tasks_dir: Path,
    task: BenchmarkTask,
    result: TaskRunResult,
    score: TaskScore,
    finish_reason: str | None = None,
    observability: dict | None = None,
) -> None:
    payload = {
        "task": task.to_dict(),
        "result": result.to_dict(),
        "score": score.to_dict(),
        # None for Configuration A (no real-provider call at all) and for
        # any task/repeat where the real provider was never reached
        # (e.g. NEEDS_CLARIFICATION, or content routed away by Phase 4
        # policy before any model call - see docs/PHASE_6_5_REAL_MODEL_REPORT.md's
        # §5 routing table). "STOP" for a normal completion.
        "finish_reason": finish_reason,
        # Latency/token-usage/retry/rate-limit-event/status detail, where
        # the provider behind this task's call exposes it - None for
        # Configuration A and any task the real provider was never reached
        # for, same as finish_reason above.
        "observability": observability,
    }
    (tasks_dir / f"{task.task_id}.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")


def _write_incomplete_task_result(
    tasks_dir: Path, task: BenchmarkTask, exc: "IncompleteCompletionError", observability: dict
) -> None:
    """Persisted in place of a normal task result when the real provider's
    completion didn't finish normally - deliberately has no "result" or
    "score" key, so it can never be mistaken for (or accidentally fed into)
    a graded outcome. See afra.providers.real_model.IncompleteCompletionError.
    """
    payload = {
        "task": task.to_dict(),
        "status": "incomplete",
        "finish_reason": exc.finish_reason,
        "partial_text": exc.partial_text,
        # Directly from the exception (this exact call's own accounting),
        # not just whatever _provider_observability(hybrid) happens to read
        # off provider state - see IncompleteCompletionError's docstring
        # and this phase's "token observability" requirement.
        "token_usage": exc.token_usage,
        "output_text_length": exc.output_text_length,
        "error": str(exc),
        "observability": observability,
    }
    (tasks_dir / f"{task.task_id}.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")


def _write_rate_limited_task_result(
    tasks_dir: Path, task: BenchmarkTask, exc: "RateLimitExhaustedError", observability: dict
) -> None:
    """Persisted in place of a normal task result when every retry allowed
    by GeminiClaimDraftProvider.max_retries still hit 429
    RESOURCE_EXHAUSTED - deliberately has no "result"/"score" key, exactly
    like _write_incomplete_task_result, so it can never be mistaken for or
    accidentally fed into a graded outcome. See
    afra.providers.real_model.RateLimitExhaustedError.
    """
    payload = {
        "task": task.to_dict(),
        "status": "rate_limited",
        "retry_count": exc.retry_count,
        "rate_limit_events": exc.rate_limit_events,
        "last_status": exc.last_status,
        "error": str(exc),
        "observability": observability,
    }
    (tasks_dir / f"{task.task_id}.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")


def _write_provider_error_task_result(
    tasks_dir: Path, task: BenchmarkTask, exc: Exception, observability: dict | None
) -> None:
    """Persisted in place of a normal task result for any other real-
    provider failure (a non-429 API error, a network failure, ...) that
    isn't IncompleteCompletionError or RateLimitExhaustedError - deliberately
    has no "result"/"score" key, same as the other two failed-attempt
    writers, so a genuinely unexpected provider failure is recorded and
    counted rather than silently dropped or crashing the whole live run.
    """
    if observability is not None:
        if observability.get("provider_status") == "ok":
            observability["provider_status"] = "provider_error"
        observability["finish_reason"] = None
        observability["token_usage"] = None
        observability["output_text_length"] = None

    payload = {
        "task": task.to_dict(),
        "status": "provider_error",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "observability": observability,
    }
    (tasks_dir / f"{task.task_id}.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")


@dataclass
class _RepeatOutcome:
    """Per-repeat counts of failed (never-graded) task attempts, rolled up
    by live_run() into the run manifest's incomplete_attempts/
    rate_limited_attempts/provider_error_attempts fields. Always all-zero
    for Configuration A (no real-provider call exists to fail this way)."""

    incomplete: int = 0
    rate_limited: int = 0
    provider_error: int = 0


def _run_repeat(
    configuration: str, tasks: list[BenchmarkTask], repeat_dir: Path, hybrid: HybridDraftClaimProvider
) -> _RepeatOutcome:
    """Runs every task in this repeat, catching each of the three ways a
    real-provider call can fail without crashing the whole run -
    IncompleteCompletionError (non-STOP finish_reason), RateLimitExhaustedError
    (429 retries exhausted), and any other exception the provider raises
    (status="provider_error", a catch-all so a genuinely unexpected failure
    is still recorded and counted rather than aborting every remaining task
    in this run). Each is handled per task, not per run, exactly like the
    existing IncompleteCompletionError handling this generalises."""
    from afra.providers.real_model import IncompleteCompletionError, RateLimitExhaustedError

    tasks_dir = repeat_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    scores: list[TaskScore] = []
    outcome = _RepeatOutcome()

    if configuration == "A":
        # Configuration A has no ModelProvider-based draft_claim call at all
        # (afra.benchmark.baseline_provider.SingleShotBaselineProvider is a
        # deterministic word-overlap scorer, not a real LLM baseline - see
        # docs/PHASE_6_REPORT.md's §12) - included for harness completeness,
        # not because Phase 6.5 proposes running it against a real model.
        baseline = SingleShotBaselineProvider()
        for task in tasks:
            result = run_configuration_a(task, baseline)
            score = grade_task(task, result)
            scores.append(score)
            _write_task_result(tasks_dir, task, result, score, finish_reason=None, observability=None)
    else:
        providers = build_full_provider_registry()
        providers[ProviderClass.EXTERNAL_STANDARD] = hybrid
        repository = Repository(repeat_dir / "tasks.db")
        run_fn = run_configuration_d if configuration == "D" else run_configuration_f
        try:
            for task in tasks:
                # Provider attribution: IncompleteCompletionError/
                # RateLimitExhaustedError can only ever be raised by
                # hybrid.real_provider itself, so those two catches below
                # are inherently correctly attributed. A *successful*
                # run_fn() call is not - purpose="draft_claim" may have been
                # served by hybrid.real_provider, by a completely different
                # provider (e.g. ENTERPRISE_APPROVED for INTERNAL content),
                # or not called at all (NEEDS_CLARIFICATION, SECURITY_BLOCKED).
                # Comparing call_count before/after is the only way to tell
                # which - without it, _provider_observability(hybrid) would
                # attribute hybrid.real_provider's *last* state (from a
                # different task) to a task it never actually touched.
                real_calls_before = getattr(hybrid.real_provider, "call_count", None)
                try:
                    result = run_fn(task, repository, providers=providers)
                except IncompleteCompletionError as exc:
                    # A genuinely failed attempt - never silently graded as
                    # a normal completion. Skips grading entirely for this
                    # task/repeat rather than fabricating a score.
                    outcome.incomplete += 1
                    _write_incomplete_task_result(tasks_dir, task, exc, _provider_observability(hybrid))
                    continue
                except RateLimitExhaustedError as exc:
                    outcome.rate_limited += 1
                    _write_rate_limited_task_result(tasks_dir, task, exc, _provider_observability(hybrid))
                    continue
                except Exception as exc:  # noqa: BLE001 - deliberate catch-all, see _run_repeat's docstring
                    outcome.provider_error += 1
                    real_calls_after = getattr(hybrid.real_provider, "call_count", None)
                    served_by_real_provider = (
                        real_calls_before is not None
                        and real_calls_after is not None
                        and real_calls_after > real_calls_before
                    )
                    observability = _provider_observability(hybrid) if served_by_real_provider else None
                    _write_provider_error_task_result(tasks_dir, task, exc, observability)
                    continue
                score = grade_task(task, result)
                scores.append(score)
                real_calls_after = getattr(hybrid.real_provider, "call_count", None)
                served_by_real_provider = (
                    real_calls_before is not None
                    and real_calls_after is not None
                    and real_calls_after > real_calls_before
                )
                observability = _provider_observability(hybrid) if served_by_real_provider else None
                _write_task_result(
                    tasks_dir,
                    task,
                    result,
                    score,
                    finish_reason=(observability["finish_reason"] if observability else None),
                    observability=observability,
                )
        finally:
            repository.close()

    config_aggregate = aggregate(configuration, scores)
    (repeat_dir / "aggregate.json").write_text(json.dumps(config_aggregate.to_dict(), indent=2) + "\n")
    return outcome


def live_run(
    *,
    suite: BenchmarkSuite,
    subset: RealModelSubset,
    results_dir: str | Path,
    configurations: tuple[str, ...] = ("F",),
    repeat_count: int = 1,
    real_provider: ModelProvider | None = None,
) -> dict:
    """Actually calls the real model - never invoked by any test or CI path
    in Phase 6.5. Fails fast, before touching the filesystem or running any
    task, if credentials are not present. See
    docs/PHASE_6_5_REAL_MODEL_REPORT.md's status section: one smoke-test
    attempt against a real API has been made (it hit a 429 this module's
    rate-limit handling - see afra.providers.real_model's module
    docstring - now guards against); the canonical experiment has never
    been executed.
    """
    _validate_configurations(configurations)
    _validate_repeat_count(repeat_count)
    real_provider = real_provider or GeminiClaimDraftProvider()
    if not real_provider.credentials_present():
        from afra.providers.real_model import RealModelCredentialsMissingError

        if hasattr(real_provider, "api_key_env") and getattr(real_provider, "api_key_env"):
            msg = (
                f"${real_provider.api_key_env} is not set - refusing to start a live run. "
                "See docs/PHASE_6_5_REAL_MODEL_REPORT.md for the live-run command."
            )
        else:
            msg = (
                f"{type(real_provider).__name__} is unavailable ('agy' CLI binary missing or unauthenticated) - "
                "refusing to start a live run."
            )
        raise RealModelCredentialsMissingError(msg)

    tasks = subset_tasks(suite, subset)
    hybrid = HybridDraftClaimProvider(real_provider)

    run_id = f"realmodel_live_{uuid.uuid4().hex[:12]}"
    run_dir = Path(results_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    incomplete_attempts = 0
    rate_limited_attempts = 0
    provider_error_attempts = 0
    for configuration in configurations:
        for repeat_index in range(repeat_count):
            repeat_dir = run_dir / configuration / f"repeat_{repeat_index:02d}"
            outcome = _run_repeat(configuration, tasks, repeat_dir, hybrid)
            incomplete_attempts += outcome.incomplete
            rate_limited_attempts += outcome.rate_limited
            provider_error_attempts += outcome.provider_error

    manifest = _manifest(
        mode="live",
        run_id=run_id,
        suite=suite,
        subset=subset,
        configurations=configurations,
        repeat_count=repeat_count,
        provider_id=hybrid.provider_id,
        model_id=hybrid.model_id,
        credentials_present=True,
        notes="LIVE real-model run - see docs/PHASE_6_5_REAL_MODEL_REPORT.md for how to interpret it.",
        incomplete_attempts=incomplete_attempts,
        rate_limited_attempts=rate_limited_attempts,
        provider_error_attempts=provider_error_attempts,
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"run_id": run_id, "run_dir": str(run_dir), "manifest": manifest}
