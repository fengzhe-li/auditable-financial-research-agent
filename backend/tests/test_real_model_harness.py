"""Phase 6.5 real-model harness tests - kept separate from core correctness
tests and from Phase 6's deterministic benchmark tests, matching this
project's established convention (docs/ROADMAP.md's Phase 6 entry).

Nothing in this file makes a network call or requires GEMINI_API_KEY to
be set - that is the whole point of Phase 6.5's dry-run/validation mode
being tested here, not the live path (afra.benchmark.real_model_harness
.live_run / afra.providers.real_model.GeminiClaimDraftProvider.complete()
with purpose="draft_claim" and real credentials), which is deliberately
never exercised by this suite or by CI.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from afra.benchmark.grader import grade_task
from afra.benchmark.real_model_harness import (
    REQUIRED_CATEGORIES,
    SUBSET_PATH,
    SUPPORTED_CONFIGURATIONS,
    TASKS_PER_CATEGORY,
    InvalidConfigurationError,
    InvalidRepeatCountError,
    derive_subset_task_ids,
    dry_run,
    live_run,
    load_subset,
    save_subset,
    subset_tasks,
    _provider_observability,
    _write_provider_error_task_result,
)
from afra.benchmark.configurations import build_full_provider_registry, run_configuration_d, run_configuration_f
from afra.domain.enums import Classification, DLPAction, ProviderClass
from afra.policy.enforcement import enforce_model_call_policy
from afra.providers.base import ModelProvider, ModelResponse
from afra.providers.real_model import (
    DEFAULT_ANTIGRAVITY_MODEL_ID,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_CALL_INTERVAL_SECONDS,
    SUPPORTED_REAL_PROVIDERS,
    AntigravityClaimDraftProvider,
    AntigravityCliError,
    GeminiClaimDraftProvider,
    HybridDraftClaimProvider,
    IncompleteCompletionError,
    RateLimitExhaustedError,
    RealModelCredentialsMissingError,
    UnsupportedPurposeError,
    build_real_provider,
)
from afra.providers.test_double import TestDoubleProvider
from afra.storage.repository import Repository
from afra.benchmark.schema import load_benchmark

BENCHMARK_PATH = Path(__file__).resolve().parents[1] / "benchmark" / "tasks_v1.json"


@pytest.fixture(scope="module")
def suite():
    return load_benchmark(BENCHMARK_PATH)


@pytest.fixture(scope="module")
def subset():
    return load_subset()


# --- Frozen subset integrity ------------------------------------------------


def test_subset_task_count_is_within_the_12_to_18_range(subset):
    assert 12 <= len(subset.task_ids) <= 18


def test_subset_has_fourteen_tasks(subset):
    assert len(subset.task_ids) == 14


def test_subset_version_and_categories(subset):
    assert subset.version == "v1"
    assert set(subset.categories) == REQUIRED_CATEGORIES


def test_subset_task_ids_are_unique(subset):
    assert len(subset.task_ids) == len(set(subset.task_ids))


def test_subset_task_ids_match_frozen_stratified_selection(suite, subset):
    """Regenerating backend/scripts/build_real_model_subset_v1.py must be
    byte-identical to the checked-in file - this is the mechanical proof."""
    assert subset.task_ids == derive_subset_task_ids(suite)


def test_every_subset_task_id_exists_in_the_benchmark_and_matches_category(suite, subset):
    by_id = {t.task_id: t for t in suite.tasks}
    for task_id in subset.task_ids:
        assert task_id in by_id
        assert by_id[task_id].category in REQUIRED_CATEGORIES


# --- All 7 required categories represented, exactly stratified -------------


def test_all_seven_required_categories_are_covered(suite, subset):
    by_id = {t.task_id: t for t in suite.tasks}
    covered = {by_id[task_id].category for task_id in subset.task_ids}
    assert covered == REQUIRED_CATEGORIES == {
        "factual_retrieval",
        "multi_document_comparison",
        "ambiguous_clarification",
        "insufficient_evidence",
        "conflicting_evidence",
        "prompt_injection",
        "sensitive_data_routing_policy",
    }


def test_temporal_change_analysis_is_deliberately_excluded(suite, subset):
    by_id = {t.task_id: t for t in suite.tasks}
    categories = {by_id[task_id].category for task_id in subset.task_ids}
    assert "temporal_change_analysis" not in categories


def test_every_category_contributes_exactly_tasks_per_category(suite, subset):
    by_id = {t.task_id: t for t in suite.tasks}
    counts = Counter(by_id[task_id].category for task_id in subset.task_ids)
    assert counts == {category: TASKS_PER_CATEGORY for category in REQUIRED_CATEGORIES}


def test_factual_retrieval_is_not_overweighted(suite, subset):
    by_id = {t.task_id: t for t in suite.tasks}
    counts = Counter(by_id[task_id].category for task_id in subset.task_ids)
    assert counts["factual_retrieval"] == max(counts.values())
    assert counts["factual_retrieval"] == min(counts.values())  # perfectly stratified


def test_ambiguous_clarification_subset_covers_both_clarification_reasons(suite, subset):
    """Both "no axis named" and "unknown company" NEEDS_CLARIFICATION
    reasons must be represented, not just one - see
    afra.benchmark.real_model_harness._CATEGORY_OVERRIDES."""
    by_id = {t.task_id: t for t in suite.tasks}
    notes = [
        by_id[task_id].notes
        for task_id in subset.task_ids
        if by_id[task_id].category == "ambiguous_clarification"
    ]
    assert any("axis named" in n for n in notes)
    assert any("known corpus" in n for n in notes)


def test_insufficient_evidence_subset_covers_both_missing_documents(suite, subset):
    by_id = {t.task_id: t for t in suite.tasks}
    missing_docs = {
        doc
        for task_id in subset.task_ids
        if by_id[task_id].category == "insufficient_evidence"
        for doc in by_id[task_id].simulate_missing_documents
    }
    assert "FABRIKAM-2025-10K" in missing_docs
    assert "CONTOSO-2025-10K" in missing_docs


def test_sensitive_data_routing_policy_subset_covers_both_allow_and_block(suite, subset):
    """policy-block accuracy is not a meaningful signal from allow-only (or
    block-only) tasks - see afra.benchmark.real_model_harness
    ._CATEGORY_OVERRIDES."""
    by_id = {t.task_id: t for t in suite.tasks}
    outcomes = {
        by_id[task_id].expected_policy_outcome
        for task_id in subset.task_ids
        if by_id[task_id].category == "sensitive_data_routing_policy"
    }
    assert "block" in outcomes
    assert "allow" in outcomes


def test_subset_round_trips_through_json(tmp_path, subset):
    out = tmp_path / "roundtrip.json"
    save_subset(subset, out)
    reloaded = load_subset(out)
    assert reloaded.to_dict() == subset.to_dict()


def test_subset_tasks_resolves_real_benchmark_tasks(suite, subset):
    tasks = subset_tasks(suite, subset)
    assert [t.task_id for t in tasks] == subset.task_ids


def test_checked_in_subset_file_exists():
    assert SUBSET_PATH.exists()


# --- Validation (configurations / repeat-count) -----------------------------


def test_supported_configurations_is_exactly_a_d_f():
    assert SUPPORTED_CONFIGURATIONS == ("A", "D", "F")


def test_dry_run_rejects_unsupported_configuration(suite, subset):
    with pytest.raises(InvalidConfigurationError):
        dry_run(suite=suite, subset=subset, configurations=("B",))


def test_dry_run_rejects_empty_configurations(suite, subset):
    with pytest.raises(InvalidConfigurationError):
        dry_run(suite=suite, subset=subset, configurations=())


def test_dry_run_rejects_non_positive_repeat_count(suite, subset):
    with pytest.raises(InvalidRepeatCountError):
        dry_run(suite=suite, subset=subset, repeat_count=0)


# --- Dry-run makes zero external calls --------------------------------------


class _ExplodingProvider(GeminiClaimDraftProvider):
    """A stand-in that fails the test if complete() is ever invoked -
    dry_run() must never call it."""

    def complete(self, purpose: str, prompt: str) -> ModelResponse:  # pragma: no cover - should never run
        raise AssertionError("dry_run() must never call provider.complete()")


def test_dry_run_never_calls_the_provider(suite, subset, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    report = dry_run(suite=suite, subset=subset, provider=_ExplodingProvider())
    assert report.task_count == 14


def test_dry_run_reports_missing_credentials_as_a_warning_not_an_error(suite, subset, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    report = dry_run(suite=suite, subset=subset, provider=GeminiClaimDraftProvider())
    assert report.manifest["credentials_present"] is False
    assert any("GEMINI_API_KEY" in w for w in report.warnings)


def test_dry_run_detects_present_credentials_without_using_them(suite, subset, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gm-not-a-real-key")
    report = dry_run(suite=suite, subset=subset, provider=_ExplodingProvider())
    assert report.manifest["credentials_present"] is True
    assert report.warnings == []


def test_dry_run_manifest_has_required_reproducibility_fields(suite, subset, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    report = dry_run(suite=suite, subset=subset)
    required = {
        "run_id", "harness_version", "mode", "benchmark_version", "subset_version",
        "subset_categories", "subset_task_ids", "configurations", "repeat_count",
        "provider_id", "model_id", "credentials_present", "created_at",
        "python_version", "git_commit", "notes",
    }
    assert required <= report.manifest.keys()
    assert report.manifest["mode"] == "dry_run"
    # Provider metadata in the manifest reflects Gemini, not the retired
    # Anthropic adapter - see this file's "provider migration" section.
    assert report.manifest["provider_id"] == "gemini-draft-claim-v1"
    assert report.manifest["model_id"] == "gemini-2.5-flash"


def test_missing_gemini_api_key_does_not_affect_normal_test_or_dry_run_behavior(suite, subset, monkeypatch):
    """The single property this whole module exists to prove: nothing here
    - construction, dry-run validation, subset/config checks - requires
    GEMINI_API_KEY. Only a genuine --live call does."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiClaimDraftProvider()
    assert provider.credentials_present() is False
    report = dry_run(suite=suite, subset=subset, provider=provider)
    assert report.task_count == 14
    assert report.manifest["credentials_present"] is False


def test_live_run_fails_fast_without_credentials_and_writes_nothing(suite, subset, tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    results_dir = tmp_path / "real_model_results"
    with pytest.raises(RealModelCredentialsMissingError):
        live_run(suite=suite, subset=subset, results_dir=results_dir, real_provider=_ExplodingProvider())
    assert not results_dir.exists()


class _AlwaysIncompleteProvider(ModelProvider):
    """Stands in for a real provider whose every draft_claim call comes
    back truncated - proves live_run()/_run_repeat() record this and keep
    going rather than crashing the whole run, with zero network calls and
    no real credentials."""

    provider_id = "always-incomplete-test-double"
    model_id = "fake-model"
    provider_class = ProviderClass.EXTERNAL_STANDARD
    allowed_data_classes = frozenset({Classification.PUBLIC})
    external_network_exposure = True

    def __init__(self) -> None:
        self.last_finish_reason = None
        self.call_count = 0

    def credentials_present(self) -> bool:
        return True

    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        from afra.providers.real_model import IncompleteCompletionError

        self.call_count += 1
        raise IncompleteCompletionError(finish_reason="MAX_TOKENS", partial_text="truncated")


def test_live_run_records_incomplete_attempts_without_crashing_the_whole_run(suite, subset, tmp_path):
    """Full-path integration test: live_run() -> _run_repeat() ->
    run_configuration_f() -> the orchestrator's real draft_claim call site
    -> a provider that always raises IncompleteCompletionError. The run
    must still complete and write a manifest recording how many attempts
    were incomplete, rather than crashing on the first one."""
    provider = _AlwaysIncompleteProvider()
    results_dir = tmp_path / "real_model_results"

    outcome = live_run(
        suite=suite, subset=subset, results_dir=results_dir,
        configurations=("F",), repeat_count=1, real_provider=provider,
    )

    assert provider.call_count > 0  # the fake provider really was reached
    assert outcome["manifest"]["incomplete_attempts"] > 0
    manifest_path = Path(outcome["run_dir"]) / "manifest.json"
    assert manifest_path.exists()

    # At least one task's persisted result reflects the incomplete status,
    # not a fabricated success.
    tasks_dir = Path(outcome["run_dir"]) / "F" / "repeat_00" / "tasks"
    incomplete_payloads = [
        json.loads(p.read_text()) for p in tasks_dir.glob("*.json")
    ]
    assert any(p.get("status") == "incomplete" for p in incomplete_payloads)
    assert all("score" not in p for p in incomplete_payloads if p.get("status") == "incomplete")


# --- GeminiClaimDraftProvider: pre-flight checks, no network required ----


def test_real_provider_construction_needs_no_credentials_or_network():
    provider = GeminiClaimDraftProvider()
    assert provider.credentials_present() is False


def test_real_provider_rejects_any_purpose_other_than_draft_claim():
    provider = GeminiClaimDraftProvider()
    with pytest.raises(UnsupportedPurposeError):
        provider.complete(purpose="plan", prompt="irrelevant")


def test_real_provider_raises_credentials_missing_before_any_import_or_network(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiClaimDraftProvider()
    with pytest.raises(RealModelCredentialsMissingError):
        provider.complete(purpose="draft_claim", prompt="irrelevant")
    assert provider.call_count == 0


def test_real_provider_declares_public_only_matching_phase_4_security_boundary():
    provider = GeminiClaimDraftProvider()
    assert provider.provider_class == ProviderClass.EXTERNAL_STANDARD
    assert provider.allowed_data_classes == frozenset({Classification.PUBLIC})
    assert provider.external_network_exposure is True


def test_gemini_adapter_default_configuration():
    """The model-selection decision recorded in
    docs/PHASE_6_5_REAL_MODEL_REPORT.md before this adapter was written:
    gemini-2.5-flash, low temperature (evidence-grounded drafting, not
    creative generation), a capped output length."""
    provider = GeminiClaimDraftProvider()
    assert provider.model_id == "gemini-2.5-flash"
    assert provider.api_key_env == "GEMINI_API_KEY"
    assert provider.provider_id == "gemini-draft-claim-v1"
    assert provider.temperature == 0.0
    assert provider.max_output_tokens == 512
    assert provider.thinking_budget == 0


# --- Gemini SDK call path, exercised against a fake module (no network,
# no real google-genai package required) ------------------------------------


def test_gemini_complete_records_latency_and_token_usage_from_a_fake_response(monkeypatch):
    """Exercises GeminiClaimDraftProvider.complete()'s actual SDK-call code
    path - client construction, generate_content(), response parsing - via
    a fake `google.genai` module injected into sys.modules, never the real
    package or a network call. Proves the adapter correctly records
    latency_ms and token usage metadata from whatever the SDK returns."""
    import sys
    import types as pytypes

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    captured: dict = {}

    class _FakeUsage:
        prompt_token_count = 42
        candidates_token_count = 17

    class _FakeResponse:
        text = "Gemini says: the claim is supported by the cited evidence."
        usage_metadata = _FakeUsage()

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            captured["model"] = model
            captured["contents"] = contents
            captured["config"] = config
            return _FakeResponse()

    class _FakeClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key
            self.models = _FakeModels()

    class _FakeGenerateContentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_types_module = pytypes.ModuleType("google.genai.types")
    fake_types_module.GenerateContentConfig = _FakeGenerateContentConfig
    fake_types_module.ThinkingConfig = lambda **kwargs: kwargs

    fake_genai_module = pytypes.ModuleType("google.genai")
    fake_genai_module.Client = _FakeClient
    fake_genai_module.types = fake_types_module

    fake_google_package = pytypes.ModuleType("google")
    fake_google_package.genai = fake_genai_module

    monkeypatch.setitem(sys.modules, "google", fake_google_package)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types_module)

    provider = GeminiClaimDraftProvider()
    response = provider.complete(purpose="draft_claim", prompt="Evidence: ...\nTask: state one claim.")

    assert response.text == "Gemini says: the claim is supported by the cited evidence."
    assert response.tokens_in == 42
    assert response.tokens_out == 17
    assert response.latency_ms >= 0
    assert response.cost == 0.0
    assert provider.call_count == 1
    assert provider.calls == [("draft_claim", "Evidence: ...\nTask: state one claim.")]
    assert captured["api_key"] == "fake-key-for-test"
    assert captured["model"] == "gemini-2.5-flash"
    assert captured["contents"] == "Evidence: ...\nTask: state one claim."
    assert captured["config"].kwargs == {
        "temperature": 0.0,
        "max_output_tokens": 512,
        "thinking_config": {"thinking_budget": 0},
    }


def _install_fake_gemini_sdk(
    monkeypatch,
    *,
    finish_reason: str | None,
    text: str = "a claim",
    thoughts_token_count: int | None = 3,
    total_token_count: int | None = 18,
):
    """Shared fake-SDK installer for finish_reason tests - builds a fake
    google.genai module whose generate_content() returns a response with
    exactly one candidate carrying the given finish_reason (as a fake enum
    member exposing `.name`, matching the real SDK's shape), and a
    usage_metadata carrying thoughts_token_count/total_token_count (as the
    real SDK's GenerateContentResponseUsageMetadata does) - defaults are
    non-None so a test can assert these are actually read/persisted, not
    just tolerated when absent."""
    import sys
    import types as pytypes

    # Assigned to a differently-named local first: a class body that both
    # reads and assigns a name shadows the enclosing function's parameter
    # of the same name for the whole class body (a real Python scoping
    # gotcha, not a typo) - `_reason` sidesteps it.
    _reason = finish_reason
    _thoughts = thoughts_token_count
    _total = total_token_count

    class _FakeFinishReason:
        name = _reason

    class _FakeCandidate:
        finish_reason = _FakeFinishReason() if _reason is not None else None

    class _FakeUsage:
        prompt_token_count = 10
        candidates_token_count = 5
        thoughts_token_count = _thoughts
        total_token_count = _total

    class _FakeResponse:
        def __init__(self):
            self.text = text
            self.usage_metadata = _FakeUsage()
            self.candidates = [_FakeCandidate()]

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            return _FakeResponse()

    class _FakeClient:
        def __init__(self, api_key):
            self.models = _FakeModels()

    fake_types_module = pytypes.ModuleType("google.genai.types")
    fake_types_module.GenerateContentConfig = lambda **kwargs: kwargs
    fake_types_module.ThinkingConfig = lambda **kwargs: kwargs

    fake_genai_module = pytypes.ModuleType("google.genai")
    fake_genai_module.Client = _FakeClient
    fake_genai_module.types = fake_types_module

    fake_google_package = pytypes.ModuleType("google")
    fake_google_package.genai = fake_genai_module

    monkeypatch.setitem(sys.modules, "google", fake_google_package)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types_module)


def test_gemini_complete_records_stop_finish_reason_on_a_normal_completion(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    _install_fake_gemini_sdk(monkeypatch, finish_reason="STOP", text="a complete claim")

    provider = GeminiClaimDraftProvider()
    response = provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert response.text == "a complete claim"
    assert provider.last_finish_reason == "STOP"


def test_gemini_complete_captures_thoughts_token_count_on_a_successful_response(monkeypatch):
    """Token observability requirement: thoughts_token_count (and the rest
    of usage_metadata) must be captured on a normal STOP response too, not
    only on an incomplete one."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    _install_fake_gemini_sdk(
        monkeypatch, finish_reason="STOP", text="a complete claim",
        thoughts_token_count=37, total_token_count=52,
    )

    provider = GeminiClaimDraftProvider()
    response = provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert response.text == "a complete claim"
    assert provider.last_call_finish_reason == "STOP"
    assert provider.last_token_usage == {
        "prompt_token_count": 10,
        "candidates_token_count": 5,
        "thoughts_token_count": 37,
        "total_token_count": 52,
    }
    assert provider.last_output_text_length == len("a complete claim")
    assert provider.last_provider_status == "ok"


def test_gemini_complete_raises_on_max_tokens_finish_reason_instead_of_returning_truncated_text(monkeypatch):
    """Gemini 2.5 Flash can spend part of max_output_tokens on internal
    "thinking" tokens before an answer - a MAX_TOKENS finish_reason means
    the completion is incomplete and must never be returned as if it were
    a normal, gradable ModelResponse."""
    from afra.providers.real_model import IncompleteCompletionError

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    _install_fake_gemini_sdk(monkeypatch, finish_reason="MAX_TOKENS", text="a truncated cl")

    provider = GeminiClaimDraftProvider()
    with pytest.raises(IncompleteCompletionError) as excinfo:
        provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert excinfo.value.finish_reason == "MAX_TOKENS"
    assert excinfo.value.partial_text == "a truncated cl"
    # last_finish_reason is only updated on a successful (STOP) call - a
    # truncated attempt must not be recorded as if it were normal.
    assert provider.last_finish_reason is None


def test_gemini_complete_captures_usage_metadata_on_a_max_tokens_incomplete_response(monkeypatch):
    """The gap this phase's diagnosis found: usage_metadata (including
    thoughts_token_count) used to be silently discarded on a MAX_TOKENS
    response, because complete() raised before ever reading it. Both the
    raised exception and the provider's own last_* state must now carry
    it."""
    from afra.providers.real_model import IncompleteCompletionError

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    _install_fake_gemini_sdk(
        monkeypatch, finish_reason="MAX_TOKENS", text="a truncated cl",
        thoughts_token_count=500, total_token_count=515,
    )

    provider = GeminiClaimDraftProvider()
    with pytest.raises(IncompleteCompletionError) as excinfo:
        provider.complete(purpose="draft_claim", prompt="irrelevant")

    expected_usage = {
        "prompt_token_count": 10,
        "candidates_token_count": 5,
        "thoughts_token_count": 500,
        "total_token_count": 515,
    }
    assert excinfo.value.token_usage == expected_usage
    assert excinfo.value.output_text_length == len("a truncated cl")
    # Provider state reflects it too, for the harness's
    # _provider_observability() to read after the exception is caught.
    assert provider.last_token_usage == expected_usage
    assert provider.last_output_text_length == len("a truncated cl")
    assert provider.last_call_finish_reason == "MAX_TOKENS"
    assert provider.last_provider_status == "incomplete"


def test_gemini_complete_treats_safety_finish_reason_as_incomplete_too(monkeypatch):
    """Any non-STOP finish_reason (not just MAX_TOKENS) is treated as an
    incomplete attempt - e.g. a SAFETY block is not a successful claim
    either."""
    from afra.providers.real_model import IncompleteCompletionError

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    _install_fake_gemini_sdk(monkeypatch, finish_reason="SAFETY", text="")

    provider = GeminiClaimDraftProvider()
    with pytest.raises(IncompleteCompletionError) as excinfo:
        provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert excinfo.value.finish_reason == "SAFETY"


def test_gemini_complete_handles_missing_usage_fields_without_crashing(monkeypatch):
    """A defensive check on the same fake-SDK path: if a response's
    usage_metadata fields come back None (seen from some SDKs/responses),
    the adapter must not crash converting them into ModelResponse's int
    fields."""
    import sys
    import types as pytypes

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    class _FakeUsage:
        prompt_token_count = None
        candidates_token_count = None

    class _FakeResponse:
        text = "A claim with no usage metadata."
        usage_metadata = _FakeUsage()

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            return _FakeResponse()

    class _FakeClient:
        def __init__(self, api_key):
            self.models = _FakeModels()

    fake_types_module = pytypes.ModuleType("google.genai.types")
    fake_types_module.GenerateContentConfig = lambda **kwargs: kwargs
    fake_types_module.ThinkingConfig = lambda **kwargs: kwargs

    fake_genai_module = pytypes.ModuleType("google.genai")
    fake_genai_module.Client = _FakeClient
    fake_genai_module.types = fake_types_module

    fake_google_package = pytypes.ModuleType("google")
    fake_google_package.genai = fake_genai_module

    monkeypatch.setitem(sys.modules, "google", fake_google_package)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types_module)

    provider = GeminiClaimDraftProvider()
    response = provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert response.tokens_in == 0
    assert response.tokens_out == 0


# --- Rate-limit pacing/retry - all against a fake SDK and injected
# sleep/clock functions, so nothing here performs a real time.sleep or
# waits any real wall-clock time, matching this file's module docstring
# guarantee (zero network calls, and now also zero real sleeping). --------


class _FakeClock:
    """A monotonic-clock stand-in that only advances when told to - never
    tied to real wall-clock time."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _fake_clock_and_sleep() -> tuple[_FakeClock, "Callable[[float], None]", list[float]]:
    """A fake clock paired with a fake sleep_fn that advances it by exactly
    the requested amount - models real time.sleep/time.monotonic's
    relationship without ever actually blocking, and records every
    requested sleep duration for assertions."""
    clock = _FakeClock()
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock.advance(seconds)

    return clock, fake_sleep, sleep_calls


class _FakeRateLimitError(Exception):
    """Stands in for google.genai.errors.ClientError on a 429
    RESOURCE_EXHAUSTED response - carries the same `code`/`status`/
    `details` shape afra.providers.real_model._is_rate_limit_error() and
    _parse_retry_delay_seconds() read, without needing the real
    google-genai package installed at all."""

    def __init__(self, retry_delay: str | None = None, retry_after_header: str | None = None) -> None:
        self.code = 429
        self.status = "RESOURCE_EXHAUSTED"
        error_details: list[dict] = []
        if retry_delay is not None:
            error_details.append(
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
            )
        self.details = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": error_details}}
        if retry_after_header is not None:
            class _FakeHttpResponse:
                headers = {"Retry-After": retry_after_header}

            self.response = _FakeHttpResponse()
        super().__init__("429 RESOURCE_EXHAUSTED. Quota exceeded.")


class _FakeServerError(Exception):
    """Stands in for google.genai.errors.ServerError on a 503 UNAVAILABLE
    (or other 5xx) response - carries the same `code`/`status`/`details`
    shape afra.providers.real_model._is_transient_server_error() reads."""

    def __init__(
        self,
        code: int = 503,
        status: str = "UNAVAILABLE",
        message: str = "This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.",
        retry_delay: str | None = None,
        retry_after_header: str | None = None,
    ) -> None:
        self.code = code
        self.status = status
        error_details: list[dict] = []
        if retry_delay is not None:
            error_details.append(
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
            )
        self.details = {"error": {"code": code, "status": status, "message": message, "details": error_details}}
        if retry_after_header is not None:
            class _FakeHttpResponse:
                headers = {"Retry-After": retry_after_header}

            self.response = _FakeHttpResponse()
        super().__init__(f"{code} {status}. {message}")


class _FakeClientError(Exception):
    """Stands in for google.genai.errors.ClientError on a deterministic 4xx
    failure (e.g. 400 INVALID_ARGUMENT, 401 UNAUTHENTICATED, 403 PERMISSION_DENIED)."""

    def __init__(self, code: int = 400, status: str = "INVALID_ARGUMENT", message: str = "Bad request.") -> None:
        self.code = code
        self.status = status
        self.details = {"error": {"code": code, "status": status, "message": message}}
        super().__init__(f"{code} {status}. {message}")


def _install_fake_gemini_sdk_with_failures(
    monkeypatch, *, failures: list[Exception], finish_reason: str = "STOP", text: str = "a claim"
) -> dict:
    """Like _install_fake_gemini_sdk, but generate_content() raises each
    exception in `failures` (in order) on the first len(failures) calls,
    then returns a normal response on every call after that - simulates a
    429 (or any other error) that eventually succeeds, or never does if a
    test only inspects the exception it ultimately raises. Returns a dict
    with a live "count" key tracking how many times generate_content() was
    actually invoked."""
    import sys
    import types as pytypes

    class _FakeFinishReason:
        name = finish_reason

    class _FakeCandidate:
        finish_reason = _FakeFinishReason()

    class _FakeUsage:
        prompt_token_count = 10
        candidates_token_count = 5

    class _FakeResponse:
        def __init__(self):
            self.text = text
            self.usage_metadata = _FakeUsage()
            self.candidates = [_FakeCandidate()]

    call_log = {"count": 0}

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            index = call_log["count"]
            call_log["count"] += 1
            if index < len(failures):
                raise failures[index]
            return _FakeResponse()

    class _FakeClient:
        def __init__(self, api_key):
            self.models = _FakeModels()

    fake_types_module = pytypes.ModuleType("google.genai.types")
    fake_types_module.GenerateContentConfig = lambda **kwargs: kwargs
    fake_types_module.ThinkingConfig = lambda **kwargs: kwargs

    fake_genai_module = pytypes.ModuleType("google.genai")
    fake_genai_module.Client = _FakeClient
    fake_genai_module.types = fake_types_module

    fake_google_package = pytypes.ModuleType("google")
    fake_google_package.genai = fake_genai_module

    monkeypatch.setitem(sys.modules, "google", fake_google_package)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types_module)
    return call_log


def test_pacing_defaults_to_fifteen_seconds_and_three_retries():
    """The requirement this whole section exists to prove: a safe default
    interval of at least 15s, and a maximum of 3 retries, without any
    caller having to configure either."""
    provider = GeminiClaimDraftProvider()
    assert provider.min_call_interval_seconds == DEFAULT_MIN_CALL_INTERVAL_SECONDS
    assert provider.min_call_interval_seconds >= 15.0
    assert provider.max_retries == DEFAULT_MAX_RETRIES == 3


def test_a_freshly_constructed_providers_first_call_never_paces(monkeypatch):
    """No previous call exists to pace against - proves deterministic/
    single-call test paths are never slowed by this patch."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(monkeypatch, failures=[])

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert sleep_calls == []


def test_pacing_sleeps_the_remaining_gap_between_two_successive_calls(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(monkeypatch, failures=[])

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, min_call_interval_seconds=15.0)
    provider.complete(purpose="draft_claim", prompt="first")
    clock.advance(4.0)  # only 4 of the required 15 seconds have "passed"
    provider.complete(purpose="draft_claim", prompt="second")

    assert sleep_calls == [11.0]  # 15 - 4 remaining


def test_pacing_does_not_sleep_if_enough_time_already_elapsed(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(monkeypatch, failures=[])

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, min_call_interval_seconds=15.0)
    provider.complete(purpose="draft_claim", prompt="first")
    clock.advance(20.0)  # already past the 15s floor
    provider.complete(purpose="draft_claim", prompt="second")

    assert sleep_calls == []


def test_min_call_interval_seconds_zero_disables_pacing_entirely(monkeypatch):
    """Deterministic providers/tests must not be slowed - setting the
    interval to 0 (what every other fake-SDK test in this file implicitly
    relies on, since they never pass sleep_fn/clock_fn at all and only ever
    make one call) opts a provider out of pacing altogether."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(monkeypatch, failures=[])

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, min_call_interval_seconds=0)
    provider.complete(purpose="draft_claim", prompt="first")
    provider.complete(purpose="draft_claim", prompt="second")

    assert sleep_calls == []


def test_pacing_state_is_per_instance_not_shared_across_providers(monkeypatch):
    """A second, independently constructed provider must not inherit
    another instance's pacing clock - proves this patch doesn't leak any
    shared/class-level pacing state that could slow down an unrelated
    provider (e.g. a fresh one built by a different test or a different
    live_run())."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(monkeypatch, failures=[])

    first = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    first.complete(purpose="draft_claim", prompt="first")

    second = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    second.complete(purpose="draft_claim", prompt="second")

    assert sleep_calls == []


def test_deterministic_test_double_provider_has_no_pacing_or_retry_state():
    """Direct proof that afra.providers.test_double.TestDoubleProvider -
    used by every deterministic benchmark path - carries none of this
    patch's new pacing/retry attributes, i.e. this patch only ever touches
    GeminiClaimDraftProvider."""
    double = TestDoubleProvider()
    for attr in ("min_call_interval_seconds", "max_retries", "last_retry_count", "last_rate_limit_events"):
        assert not hasattr(double, attr)


def test_gemini_complete_retries_once_on_429_then_succeeds(monkeypatch):
    """No server-provided delay - bounded exponential backoff (10s for the
    first retry) is used, but floored at min_call_interval_seconds (15.0
    default) per this phase's retry-pacing fix, so the actual sleep is 15.0,
    not the raw 10s backoff value."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(monkeypatch, failures=[_FakeRateLimitError()])

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    response = provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert response.text == "a claim"
    assert provider.last_provider_status == "ok"
    assert provider.last_retry_count == 1
    assert len(provider.last_rate_limit_events) == 1
    assert provider.last_rate_limit_events[0] == {
        "attempt": 1, "delay_seconds": 15.0, "server_delay_seconds": None,
        "source": "backoff", "status": "RESOURCE_EXHAUSTED",
    }
    assert sleep_calls == [15.0]  # backoff (10.0) floored to min_call_interval_seconds (15.0)


def test_gemini_complete_retries_repeatedly_on_repeated_429_then_succeeds(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(
        monkeypatch, failures=[_FakeRateLimitError(), _FakeRateLimitError()]
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    response = provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert response.text == "a claim"
    assert provider.last_retry_count == 2
    # 10s backoff floored to 15.0; 20s backoff already exceeds the floor,
    # so it passes through unchanged.
    assert [e["delay_seconds"] for e in provider.last_rate_limit_events] == [15.0, 20.0]
    assert sleep_calls == [15.0, 20.0]


def test_no_hint_backoff_below_the_floor_is_raised_to_the_minimum_interval(monkeypatch):
    """Requirement: without a server-provided delay, bounded exponential
    backoff remains allowed, but the retry delay must still be at least
    min_call_interval_seconds - proven directly against a non-default,
    larger interval so the floor (not the backoff schedule) is clearly
    what determines the sleep."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(monkeypatch, failures=[_FakeRateLimitError()])

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, min_call_interval_seconds=25.0)
    provider.complete(purpose="draft_claim", prompt="irrelevant")

    # 10s backoff would normally apply for the first retry - floored to 25.0.
    assert sleep_calls == [25.0]
    assert provider.last_rate_limit_events[0]["delay_seconds"] == 25.0
    assert provider.last_rate_limit_events[0]["source"] == "backoff"


def test_gemini_complete_raises_rate_limit_exhausted_after_max_retries(monkeypatch):
    """Maximum 3 retries per provider call - a 429 on every single attempt
    (including all 3 retries) must give up rather than retry forever."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    call_log = _install_fake_gemini_sdk_with_failures(
        monkeypatch, failures=[_FakeRateLimitError() for _ in range(10)]
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, max_retries=3)
    with pytest.raises(RateLimitExhaustedError) as excinfo:
        provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert excinfo.value.retry_count == 3
    assert excinfo.value.last_status == "RESOURCE_EXHAUSTED"
    assert len(excinfo.value.rate_limit_events) == 3
    assert call_log["count"] == 4  # 1 initial attempt + 3 retries, never a 5th
    assert provider.last_provider_status == "rate_limited"
    assert provider.last_retry_count == 3
    assert provider.last_rate_limit_events == excinfo.value.rate_limit_events


def test_server_retry_delay_of_zero_seconds_still_sleeps_at_least_the_minimum_interval(monkeypatch):
    """The exact failure mode diagnosed in run realmodel_live_8ddec45b00dd:
    Gemini reported a RetryInfo delay of "0s" on consecutive 429s within an
    already-saturated quota window, and retrying on that literal value
    produced another 429 immediately. A "0s" server delay must never be
    trusted below min_call_interval_seconds."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(
        monkeypatch, failures=[_FakeRateLimitError(retry_delay="0s")]
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    provider.complete(purpose="draft_claim", prompt="irrelevant")

    # max(0.0, 15.0) + 2.0 margin = 17.0 - never the raw 0s.
    assert sleep_calls == [17.0]
    assert provider.last_rate_limit_events[0] == {
        "attempt": 1, "delay_seconds": 17.0, "server_delay_seconds": 0.0,
        "source": "server", "status": "RESOURCE_EXHAUSTED",
    }


def test_server_retry_delay_below_the_minimum_interval_is_floored(monkeypatch):
    """A RetryInfo delay of 3s (below the 15s pacing floor) must be raised
    to the floor plus safety margin, not used literally - see this
    module's docstring's "Rate-limit handling" section."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(
        monkeypatch, failures=[_FakeRateLimitError(retry_delay="3s")]
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    provider.complete(purpose="draft_claim", prompt="irrelevant")

    # max(3.0, 15.0) + 2.0 margin = 17.0, not the raw 3s.
    assert sleep_calls == [17.0]
    assert provider.last_rate_limit_events[0]["source"] == "server"
    assert provider.last_rate_limit_events[0]["server_delay_seconds"] == 3.0
    assert provider.last_rate_limit_events[0]["delay_seconds"] == 17.0


def test_server_retry_delay_above_the_minimum_interval_is_respected_plus_margin(monkeypatch):
    """A RetryInfo delay already above the pacing floor (45s, matching what
    a saturated-quota live run actually reported) is honoured, with the
    safety margin still added on top - the floor never *shortens* a longer
    server-requested wait."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(
        monkeypatch, failures=[_FakeRateLimitError(retry_delay="45s")]
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    provider.complete(purpose="draft_claim", prompt="irrelevant")

    # max(45.0, 15.0) + 2.0 margin = 47.0.
    assert sleep_calls == [47.0]
    assert provider.last_rate_limit_events[0]["server_delay_seconds"] == 45.0
    assert provider.last_rate_limit_events[0]["delay_seconds"] == 47.0


def test_gemini_complete_honors_retry_after_header_when_no_retry_info_present(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(
        monkeypatch, failures=[_FakeRateLimitError(retry_after_header="7")]
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    provider.complete(purpose="draft_claim", prompt="irrelevant")

    # max(7.0, 15.0) + 2.0 margin = 17.0, not the raw 7s header value.
    assert sleep_calls == [17.0]
    assert provider.last_rate_limit_events[0]["source"] == "server"
    assert provider.last_rate_limit_events[0]["server_delay_seconds"] == 7.0


def test_gemini_complete_does_not_retry_a_non_rate_limit_error(monkeypatch):
    """Only 429 RESOURCE_EXHAUSTED is retried - any other exception (a
    different APIError, a network failure, ...) propagates immediately, on
    the first attempt, with zero sleeping - afra.benchmark
    .real_model_harness catches these at the task level instead
    (status="provider_error")."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    call_log = _install_fake_gemini_sdk_with_failures(
        monkeypatch, failures=[ValueError("not a rate limit error")]
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    with pytest.raises(ValueError, match="not a rate limit error"):
        provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert call_log["count"] == 1
    assert sleep_calls == []


def test_retry_pacing_is_not_double_stacked_with_the_initial_pacing_wait(monkeypatch):
    """The single unified _pace() mechanism means an effective retry delay
    is slept exactly once - not the initial min_call_interval_seconds
    pacing wait, *then* a separate, additional retry-delay sleep on top of
    it. A first attempt (nothing to pace against yet) that fails with a
    20s server delay must sleep exactly max(20,15)+2=22.0 once, not
    15.0 + 22.0."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(
        monkeypatch, failures=[_FakeRateLimitError(retry_delay="20s")]
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, min_call_interval_seconds=15.0)
    provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert sleep_calls == [22.0]  # not [15.0, 22.0] or [22.0, 15.0]


def test_retry_attempts_are_paced_via_real_elapsed_time_not_a_step_counter(monkeypatch):
    """"Retry attempt also goes through global pacing", proven with a
    clock that genuinely advances during the failed attempt itself (e.g. a
    slow HTTP round trip before the 429 response arrives), not just via
    fake_sleep's bookkeeping - _pace() before the retry must compute its
    wait from the *actual* elapsed time on the shared clock (measured from
    when the failed attempt concluded), not from a naive "always sleep the
    full effective_delay" assumption that ignores the clock entirely."""
    import sys
    import types as pytypes

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()

    class _FakeFinishReason:
        name = "STOP"

    class _FakeCandidate:
        finish_reason = _FakeFinishReason()

    class _FakeUsage:
        prompt_token_count = 10
        candidates_token_count = 5

    class _FakeResponse:
        text = "a claim"
        usage_metadata = _FakeUsage()
        candidates = [_FakeCandidate()]

    call_log = {"count": 0}

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            index = call_log["count"]
            call_log["count"] += 1
            if index == 0:
                clock.advance(5.0)  # a slow 5s HTTP round trip before the 429 arrives
                raise _FakeRateLimitError(retry_delay="17s")  # already above the 15s floor
            return _FakeResponse()

    class _FakeClient:
        def __init__(self, api_key):
            self.models = _FakeModels()

    fake_types_module = pytypes.ModuleType("google.genai.types")
    fake_types_module.GenerateContentConfig = lambda **kwargs: kwargs
    fake_types_module.ThinkingConfig = lambda **kwargs: kwargs
    fake_genai_module = pytypes.ModuleType("google.genai")
    fake_genai_module.Client = _FakeClient
    fake_genai_module.types = fake_types_module
    fake_google_package = pytypes.ModuleType("google")
    fake_google_package.genai = fake_genai_module
    monkeypatch.setitem(sys.modules, "google", fake_google_package)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types_module)

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    provider.complete(purpose="draft_claim", prompt="irrelevant")

    # effective_delay = max(17, 15) + 2 = 19.0, measured from when the
    # failed attempt concluded (clock=5.0, after its own 5s "HTTP round
    # trip") - the retry's _pace() call correctly reads the clock at that
    # point rather than assuming zero elapsed time or double-counting the
    # 5s the attempt itself took.
    assert sleep_calls == [19.0]
    assert provider.last_rate_limit_events[0]["server_delay_seconds"] == 17.0
    assert provider.last_rate_limit_events[0]["delay_seconds"] == 19.0


def test_global_last_call_timestamp_updates_after_a_failed_429_attempt(monkeypatch):
    """The shared _last_call_at clock must advance even when an attempt
    fails (429) and is never retried (max_retries=0 here, for a minimal
    repro) - otherwise a later complete() call (a different task) would
    wrongly treat a just-failed attempt as if it never happened and skip
    pacing against it entirely."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(monkeypatch, failures=[_FakeRateLimitError()])

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, max_retries=0)
    with pytest.raises(RateLimitExhaustedError):
        provider.complete(purpose="draft_claim", prompt="first")

    assert provider._last_call_at is not None
    assert sleep_calls == []  # this instance's very first attempt ever - nothing to pace against yet

    # A second complete() call (e.g. the next task) must pace against the
    # first call's failed attempt, not skip pacing because "no call has
    # succeeded yet".
    response = provider.complete(purpose="draft_claim", prompt="second")
    assert response.text == "a claim"
    assert sleep_calls == [15.0]


def test_no_real_sleeping_occurs_across_a_full_retry_and_pacing_scenario(monkeypatch):
    """Mechanical proof for this phase's explicit "no real sleeping in unit
    tests" requirement: a scenario that would, under real time.sleep, take
    at least 15 (pacing) + 10 + 20 (backoff retries) = 45 real seconds
    completes near-instantly because sleep_fn/clock_fn are fakes."""
    import time as real_time

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(
        monkeypatch, failures=[_FakeRateLimitError(), _FakeRateLimitError()]
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, min_call_interval_seconds=15.0)
    provider.complete(purpose="draft_claim", prompt="first")  # paces 0 (first call)

    started = real_time.perf_counter()
    provider.complete(purpose="draft_claim", prompt="second")  # would pace + retry twice if real
    wall_elapsed = real_time.perf_counter() - started

    assert wall_elapsed < 1.0
    assert sum(sleep_calls) >= 15.0  # the fake clock still recorded the full paced/backoff duration


def test_is_rate_limit_error_detects_by_code_or_by_status():
    import afra.providers.real_model as real_model_module

    class _CodeOnly:
        code = 429

    class _StatusOnly:
        status = "RESOURCE_EXHAUSTED"

    class _Neither:
        code = 400
        status = "INVALID_ARGUMENT"

    assert real_model_module._is_rate_limit_error(_CodeOnly()) is True
    assert real_model_module._is_rate_limit_error(_StatusOnly()) is True
    assert real_model_module._is_rate_limit_error(_Neither()) is False
    assert real_model_module._is_rate_limit_error(ValueError("plain exception, no attrs")) is False


def test_parse_retry_delay_seconds_reads_the_top_level_details_shape():
    """Some response shapes put `details` (the RetryInfo list) directly on
    the parsed error dict rather than nested under an "error" key - both
    are real shapes the API's JSON body can take (see
    _parse_retry_delay_seconds()'s docstring)."""
    import afra.providers.real_model as real_model_module

    class _Exc(Exception):
        details = {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}]}

    assert real_model_module._parse_retry_delay_seconds(_Exc()) == 12.0


def test_parse_retry_delay_seconds_returns_none_when_nothing_usable_is_present():
    import afra.providers.real_model as real_model_module

    class _Exc(Exception):
        details = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": []}}

    assert real_model_module._parse_retry_delay_seconds(_Exc()) is None
    assert real_model_module._parse_retry_delay_seconds(ValueError("no details at all")) is None


# --- Harness-level rate-limit / provider-error handling --------------------


class _AlwaysRateLimitedProvider(ModelProvider):
    """Stands in for a real provider whose every draft_claim call exhausts
    its rate-limit retries - proves live_run()/_run_repeat() record this
    and keep going rather than crashing the whole run, with zero network
    calls, no real credentials, and no real sleeping."""

    provider_id = "always-rate-limited-test-double"
    model_id = "fake-model"
    provider_class = ProviderClass.EXTERNAL_STANDARD
    allowed_data_classes = frozenset({Classification.PUBLIC})
    external_network_exposure = True

    def __init__(self) -> None:
        self.call_count = 0
        self.last_finish_reason = None
        self.last_retry_count = 0
        self.last_rate_limit_events: list[dict] = []
        self.last_provider_status = "unknown"
        self.last_latency_ms = None
        self.last_token_usage = None

    def credentials_present(self) -> bool:
        return True

    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        self.call_count += 1
        events = [{"attempt": 1, "delay_seconds": 10.0, "source": "backoff", "status": "RESOURCE_EXHAUSTED"}]
        self.last_retry_count = 3
        self.last_rate_limit_events = events
        self.last_provider_status = "rate_limited"
        self.last_latency_ms = 42
        raise RateLimitExhaustedError(retry_count=3, rate_limit_events=events, last_status="RESOURCE_EXHAUSTED")


class _AlwaysProviderErrorProvider(ModelProvider):
    """Stands in for a real provider whose every draft_claim call fails
    with some other (non-429, non-incomplete) error - proves the harness's
    catch-all still records and counts it, rather than crashing the whole
    live run or silently dropping it."""

    provider_id = "always-provider-error-test-double"
    model_id = "fake-model"
    provider_class = ProviderClass.EXTERNAL_STANDARD
    allowed_data_classes = frozenset({Classification.PUBLIC})
    external_network_exposure = True

    def __init__(self) -> None:
        self.call_count = 0

    def credentials_present(self) -> bool:
        return True

    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        self.call_count += 1
        raise ConnectionError("simulated network failure, not a rate limit or incomplete-completion error")


def test_live_run_records_rate_limited_attempts_without_crashing_the_whole_run(suite, subset, tmp_path):
    provider = _AlwaysRateLimitedProvider()
    results_dir = tmp_path / "real_model_results"

    outcome = live_run(
        suite=suite, subset=subset, results_dir=results_dir,
        configurations=("F",), repeat_count=1, real_provider=provider,
    )

    assert provider.call_count > 0
    assert outcome["manifest"]["rate_limited_attempts"] > 0
    assert outcome["manifest"]["incomplete_attempts"] == 0
    assert outcome["manifest"]["provider_error_attempts"] == 0

    tasks_dir = Path(outcome["run_dir"]) / "F" / "repeat_00" / "tasks"
    payloads = [json.loads(p.read_text()) for p in tasks_dir.glob("*.json")]
    rate_limited = [p for p in payloads if p.get("status") == "rate_limited"]
    assert rate_limited
    assert all("score" not in p for p in rate_limited)
    assert all("result" not in p for p in rate_limited)
    assert rate_limited[0]["retry_count"] == 3
    assert rate_limited[0]["observability"]["provider_status"] == "rate_limited"


def test_live_run_records_provider_error_attempts_without_crashing_the_whole_run(suite, subset, tmp_path):
    provider = _AlwaysProviderErrorProvider()
    results_dir = tmp_path / "real_model_results"

    outcome = live_run(
        suite=suite, subset=subset, results_dir=results_dir,
        configurations=("F",), repeat_count=1, real_provider=provider,
    )

    assert provider.call_count > 0
    assert outcome["manifest"]["provider_error_attempts"] > 0
    assert outcome["manifest"]["rate_limited_attempts"] == 0
    assert outcome["manifest"]["incomplete_attempts"] == 0

    tasks_dir = Path(outcome["run_dir"]) / "F" / "repeat_00" / "tasks"
    payloads = [json.loads(p.read_text()) for p in tasks_dir.glob("*.json")]
    failed = [p for p in payloads if p.get("status") == "provider_error"]
    assert failed
    assert all("score" not in p for p in failed)
    assert failed[0]["error_type"] == "ConnectionError"
    assert "simulated network failure" in failed[0]["error"]


def test_live_run_manifest_counts_default_to_zero_for_a_clean_run(suite, subset, tmp_path):
    stub = _StubRealProvider()
    results_dir = tmp_path / "real_model_results"

    outcome = live_run(
        suite=suite, subset=subset, results_dir=results_dir,
        configurations=("F",), repeat_count=1, real_provider=stub,
    )

    assert outcome["manifest"]["incomplete_attempts"] == 0
    assert outcome["manifest"]["rate_limited_attempts"] == 0
    assert outcome["manifest"]["provider_error_attempts"] == 0


def test_successful_task_result_includes_observability_fields(suite, subset, tmp_path):
    """Record observability fields where available - latency, finish_reason,
    token usage, retry_count, rate_limit_events, final provider status -
    even on a successful (graded) task result, not only on a failed one."""

    class _ObservableStubProvider(ModelProvider):
        provider_id = "observable-stub"
        model_id = "stub-model"
        provider_class = ProviderClass.EXTERNAL_STANDARD
        allowed_data_classes = frozenset({Classification.PUBLIC})
        external_network_exposure = True

        def __init__(self) -> None:
            self.call_count = 0
            self.last_finish_reason = "STOP"
            self.last_call_finish_reason = "STOP"
            self.last_latency_ms = 123
            self.last_token_usage = {
                "prompt_token_count": 10,
                "candidates_token_count": 5,
                "thoughts_token_count": 0,
                "total_token_count": 15,
            }
            self.last_output_text_length = 42
            self.last_retry_count = 1
            self.last_rate_limit_events = [{"attempt": 1, "delay_seconds": 10.0, "source": "backoff", "status": "RESOURCE_EXHAUSTED"}]
            self.last_provider_status = "ok"

        def credentials_present(self) -> bool:
            return True

        def complete(self, purpose: str, prompt: str) -> ModelResponse:
            self.call_count += 1
            return ModelResponse(text=f"REAL:{prompt}", tokens_in=10, tokens_out=5, latency_ms=123, cost=0.0)

    provider = _ObservableStubProvider()
    results_dir = tmp_path / "real_model_results"

    outcome = live_run(
        suite=suite, subset=subset, results_dir=results_dir,
        configurations=("F",), repeat_count=1, real_provider=provider,
    )

    # FR-01 is one of the 10 subset tasks known to actually reach the real
    # provider slot under Configuration F (see
    # test_real_provider_slot_is_only_reached_for_genuinely_public_subset_tasks) -
    # picked by task_id rather than "the first task written", since an
    # AC-01/AC-04/SD-01/SD-03 task's observability would just reflect
    # whatever the real provider's *last* actual call left behind, not this
    # task's own (it never reaches the real provider at all).
    payload = json.loads((Path(outcome["run_dir"]) / "F" / "repeat_00" / "tasks" / "FR-01.json").read_text())
    assert "score" in payload
    observability = payload["observability"]
    assert observability["finish_reason"] == "STOP"
    assert observability["latency_ms"] == 123
    assert observability["token_usage"] == {
        "prompt_token_count": 10,
        "candidates_token_count": 5,
        "thoughts_token_count": 0,
        "total_token_count": 15,
    }
    assert observability["output_text_length"] == 42
    assert observability["retry_count"] == 1
    assert observability["rate_limit_events"] == [
        {"attempt": 1, "delay_seconds": 10.0, "source": "backoff", "status": "RESOURCE_EXHAUSTED"}
    ]
    assert observability["provider_status"] == "ok"


def test_configuration_a_task_results_have_no_observability_data(suite, subset, tmp_path):
    """Configuration A never calls a real provider at all
    (afra.benchmark.baseline_provider.SingleShotBaselineProvider is
    deterministic) - its task results' observability field must stay None,
    not be fabricated."""
    results_dir = tmp_path / "real_model_results"

    outcome = live_run(
        suite=suite, subset=subset, results_dir=results_dir,
        configurations=("A",), repeat_count=1, real_provider=_StubRealProvider(),
    )

    tasks_dir = Path(outcome["run_dir"]) / "A" / "repeat_00" / "tasks"
    payloads = [json.loads(p.read_text()) for p in tasks_dir.glob("*.json")]
    assert payloads
    assert all(p["observability"] is None for p in payloads)
    assert all(p["finish_reason"] is None for p in payloads)


class _StatefulStubRealProvider(ModelProvider):
    """Unlike _StubRealProvider, mimics GeminiClaimDraftProvider's own
    behaviour of updating last_* state on every call - needed to prove the
    provider-attribution fix, since a stub with no last_* state at all
    can't demonstrate the bug it fixes (stale data being read for a task
    that never called it) - there'd be nothing to leak."""

    provider_id = "stateful-stub-real"
    model_id = "stub-model"
    provider_class = ProviderClass.EXTERNAL_STANDARD
    allowed_data_classes = frozenset({Classification.PUBLIC})
    external_network_exposure = True

    def __init__(self) -> None:
        self.call_count = 0
        self.last_call_finish_reason: str | None = None
        self.last_token_usage: dict | None = None
        self.last_output_text_length: int | None = None
        self.last_latency_ms: int | None = None
        self.last_retry_count = 0
        self.last_rate_limit_events: list[dict] = []
        self.last_provider_status = "unknown"

    def credentials_present(self) -> bool:
        return True

    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        self.call_count += 1
        self.last_call_finish_reason = "STOP"
        self.last_token_usage = {
            "prompt_token_count": 10,
            "candidates_token_count": 5,
            "thoughts_token_count": 0,
            "total_token_count": 15,
        }
        self.last_output_text_length = 42
        self.last_latency_ms = 100
        self.last_provider_status = "ok"
        return ModelResponse(text=f"REAL:{prompt}", tokens_in=10, tokens_out=5, latency_ms=100, cost=0.0)


def test_deterministic_test_double_served_task_does_not_inherit_stale_gemini_metadata(suite, subset, tmp_path):
    """The exact bug this phase's diagnosis found: SD-01 (INTERNAL
    classification) is routed by Phase 4 enforcement to the deterministic
    ENTERPRISE_APPROVED test double, never to hybrid.real_provider at all -
    but earlier subset tasks (FR-01 etc., PUBLIC classification) *do* call
    the real provider and leave it in a "STOP"/populated state. Before the
    attribution fix, SD-01's persisted result would read that stale state
    off hybrid.real_provider and misreport it as if SD-01 itself had made a
    real Gemini call. After the fix, SD-01's observability/finish_reason
    must be None."""
    provider = _StatefulStubRealProvider()
    results_dir = tmp_path / "real_model_results"

    outcome = live_run(
        suite=suite, subset=subset, results_dir=results_dir,
        configurations=("F",), repeat_count=1, real_provider=provider,
    )

    # Sanity check: the real provider really was called for earlier tasks,
    # so there is genuinely stale state on it by the time SD-01 runs.
    assert provider.call_count > 0
    assert provider.last_call_finish_reason == "STOP"

    sd01_payload = json.loads(
        (Path(outcome["run_dir"]) / "F" / "repeat_00" / "tasks" / "SD-01.json").read_text()
    )
    assert sd01_payload["observability"] is None
    assert sd01_payload["finish_reason"] is None

    # Contrast: a task that genuinely was served by the real provider still
    # gets its (correctly attributed) observability.
    fr01_payload = json.loads(
        (Path(outcome["run_dir"]) / "F" / "repeat_00" / "tasks" / "FR-01.json").read_text()
    )
    assert fr01_payload["observability"] is not None
    assert fr01_payload["observability"]["finish_reason"] == "STOP"


def test_deterministic_benchmark_is_unaffected_by_the_rate_limit_patch(suite, tmp_path):
    """The canonical deterministic benchmark path (afra.benchmark
    .configurations with no providers override, i.e. exactly what
    scripts/run_benchmark.py runs) must still work identically - this
    patch only ever touches afra.providers.real_model, never
    afra.providers.test_double or afra.benchmark.configurations'
    defaults."""
    task = next(t for t in suite.tasks if t.task_id == "FR-01")
    repo = Repository(tmp_path / "deterministic_unaffected.db")
    try:
        result_f = run_configuration_f(task, repo)
    finally:
        repo.close()
    assert result_f.error is None
    assert grade_task(task, result_f).completed is True


# --- Provider migration: Anthropic retired, Gemini is canonical ------------


def test_anthropic_is_no_longer_the_canonical_real_provider():
    import afra.providers.real_model as real_model_module

    assert not hasattr(real_model_module, "AnthropicClaimDraftProvider")
    assert hasattr(real_model_module, "GeminiClaimDraftProvider")
    assert real_model_module.DEFAULT_API_KEY_ENV == "GEMINI_API_KEY"
    assert real_model_module.DEFAULT_MODEL_ID == "gemini-2.5-flash"


def test_real_model_harness_defaults_to_the_gemini_provider(suite, subset, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    report = dry_run(suite=suite, subset=subset)
    assert report.manifest["provider_id"] == "gemini-draft-claim-v1"


# --- HybridDraftClaimProvider ------------------------------------------------


class _StubRealProvider(ModelProvider):
    provider_id = "stub-real"
    model_id = "stub-model"
    provider_class = ProviderClass.EXTERNAL_STANDARD
    allowed_data_classes = frozenset({Classification.PUBLIC})
    external_network_exposure = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.call_count = 0

    def credentials_present(self) -> bool:
        return True

    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        self.calls.append((purpose, prompt))
        self.call_count += 1
        return ModelResponse(text=f"REAL:{prompt}", tokens_in=1, tokens_out=1, latency_ms=1, cost=0.0)


def test_hybrid_routes_draft_claim_to_the_real_provider():
    real = _StubRealProvider()
    fallback = TestDoubleProvider()
    hybrid = HybridDraftClaimProvider(real, fallback)

    response = hybrid.complete(purpose="draft_claim", prompt="the prompt")

    assert response.text == "REAL:the prompt"
    assert real.calls == [("draft_claim", "the prompt")]
    assert fallback.call_count == 0


def test_hybrid_routes_every_other_purpose_to_the_deterministic_fallback():
    real = _StubRealProvider()
    fallback = TestDoubleProvider()
    hybrid = HybridDraftClaimProvider(real, fallback)

    response = hybrid.complete(purpose="interpret", prompt="Research question: does X exist?")

    assert real.calls == []
    assert fallback.call_count == 1
    assert response.text.startswith("COMPANIES:")  # TestDoubleProvider's deterministic _interpret() shape


def test_hybrid_declares_public_only_matching_the_real_provider_it_wraps():
    hybrid = HybridDraftClaimProvider(_StubRealProvider())
    assert hybrid.provider_class == ProviderClass.EXTERNAL_STANDARD
    assert hybrid.allowed_data_classes == frozenset({Classification.PUBLIC})


# --- Phase 4 security boundaries preserved when the hybrid is registered ---


def test_restricted_content_is_still_blocked_with_the_hybrid_provider_registered():
    hybrid = HybridDraftClaimProvider(_StubRealProvider())
    decision = enforce_model_call_policy(
        classification=Classification.RESTRICTED,
        prompt="irrelevant",
        requested_provider_class=ProviderClass.EXTERNAL_STANDARD,
        providers={ProviderClass.EXTERNAL_STANDARD: hybrid},
    )
    assert decision.allowed is False
    assert decision.action == DLPAction.BLOCK


def test_public_content_is_allowed_through_the_hybrid_provider():
    hybrid = HybridDraftClaimProvider(_StubRealProvider())
    decision = enforce_model_call_policy(
        classification=Classification.PUBLIC,
        prompt="irrelevant",
        requested_provider_class=ProviderClass.EXTERNAL_STANDARD,
        providers={ProviderClass.EXTERNAL_STANDARD: hybrid},
    )
    assert decision.allowed is True
    assert decision.selected_provider_class == ProviderClass.EXTERNAL_STANDARD


def test_confidential_content_is_not_eligible_for_the_hybrid_provider_alone():
    """The hybrid declares allowed_data_classes={PUBLIC} only, same as the
    real adapter it wraps - CONFIDENTIAL content must not be routed to it
    even if no other provider is registered to catch the fallback."""
    hybrid = HybridDraftClaimProvider(_StubRealProvider())
    decision = enforce_model_call_policy(
        classification=Classification.CONFIDENTIAL,
        prompt="irrelevant",
        requested_provider_class=ProviderClass.EXTERNAL_STANDARD,
        providers={ProviderClass.EXTERNAL_STANDARD: hybrid},
    )
    assert decision.allowed is False


# --- run_configuration_d/f stay backward compatible with providers=None ----


def test_run_configuration_d_and_f_default_providers_unchanged(suite, tmp_path):
    task = next(t for t in suite.tasks if t.task_id == "FR-01")
    repo = Repository(tmp_path / "backcompat.db")
    try:
        result_d = run_configuration_d(task, repo)
        result_f = run_configuration_f(task, repo)
    finally:
        repo.close()
    assert result_d.error is None
    assert result_f.error is None
    assert grade_task(task, result_f).completed is True


# --- End-to-end: which subset tasks actually reach the real provider slot --


def test_real_provider_slot_is_only_reached_for_genuinely_public_subset_tasks(suite, subset, tmp_path):
    """Mechanically verifies, with a stub standing in for the real adapter
    (no network, no credentials - the same substitution afra.providers
    .real_model.HybridDraftClaimProvider makes for a live run), which of the
    14 frozen subset tasks actually invoke it under Configuration F.
    ambiguous_clarification tasks stop at NEEDS_CLARIFICATION before any
    claim-drafting call; sensitive_data_routing_policy tasks are routed
    around the real provider slot entirely by Phase 4's unmodified
    enforcement (INTERNAL -> ENTERPRISE_APPROVED test double, RESTRICTED ->
    blocked) - both by construction, not by anything this harness special-
    cases. See docs/PHASE_6_5_REAL_MODEL_REPORT.md's §5/§8.
    """
    stub = _StubRealProvider()
    hybrid = HybridDraftClaimProvider(stub)
    tasks = subset_tasks(suite, subset)
    repo = Repository(tmp_path / "real_provider_reach.db")
    calls_by_task: dict[str, int] = {}
    try:
        for task in tasks:
            before = len(stub.calls)
            providers = build_full_provider_registry()
            providers[ProviderClass.EXTERNAL_STANDARD] = hybrid
            run_configuration_f(task, repo, providers=providers)
            calls_by_task[task.task_id] = len(stub.calls) - before
    finally:
        repo.close()

    never_reached = {task_id for task_id, count in calls_by_task.items() if count == 0}
    assert never_reached == {"AC-01", "AC-04", "SD-01", "SD-03"}
    assert sum(calls_by_task.values()) > 0


# --- Transient server error (5xx) retry & observability tests -------------


def test_gemini_complete_retries_once_on_503_then_succeeds(monkeypatch):
    """503 UNAVAILABLE (transient server error) is retried. If the retry succeeds,
    the call succeeds normally, retry_count is 1, and provider_status is 'ok'."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(
        monkeypatch, failures=[_FakeServerError(503, "UNAVAILABLE")]
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    response = provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert response.text == "a claim"
    assert provider.last_provider_status == "ok"
    assert provider.last_retry_count == 1
    assert len(provider.last_rate_limit_events) == 1
    assert provider.last_rate_limit_events[0] == {
        "attempt": 1,
        "delay_seconds": 15.0,
        "server_delay_seconds": None,
        "source": "backoff",
        "status": "UNAVAILABLE",
    }
    assert sleep_calls == [15.0]


def test_gemini_complete_repeated_503_exhausts_retries_and_records_observability(monkeypatch):
    """Repeated 503 UNAVAILABLE errors exhaust max_retries, re-raise the underlying
    ServerError, and record provider_status='provider_error' with accurate retry_count
    and rate_limit_events, clearing successful-call metadata."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    failures = [_FakeServerError(503, "UNAVAILABLE") for _ in range(5)]
    call_log = _install_fake_gemini_sdk_with_failures(monkeypatch, failures=failures)

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, max_retries=2)
    with pytest.raises(_FakeServerError) as excinfo:
        provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert excinfo.value.code == 503
    assert call_log["count"] == 3  # 1 initial + 2 retries
    assert sleep_calls == [15.0, 20.0]  # attempt 1: 10s floored to 15s; attempt 2: 20s
    assert provider.last_provider_status == "provider_error"
    assert provider.last_retry_count == 2
    assert len(provider.last_rate_limit_events) == 2
    assert provider.last_call_finish_reason is None
    assert provider.last_token_usage is None
    assert provider.last_output_text_length is None


def test_pacing_is_applied_to_each_503_retry(monkeypatch):
    """503 retries respect server-provided retry hints when present, adding safety margin
    and flooring at min_call_interval_seconds."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    _install_fake_gemini_sdk_with_failures(
        monkeypatch,
        failures=[_FakeServerError(503, retry_delay="35s")],
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock)
    provider.complete(purpose="draft_claim", prompt="irrelevant")

    # max(35.0, 15.0) + 2.0 = 37.0
    assert sleep_calls == [37.0]
    assert provider.last_rate_limit_events[0]["source"] == "server"
    assert provider.last_rate_limit_events[0]["server_delay_seconds"] == 35.0
    assert provider.last_rate_limit_events[0]["delay_seconds"] == 37.0


@pytest.mark.parametrize("status_code,status_name", [
    (400, "INVALID_ARGUMENT"),
    (401, "UNAUTHENTICATED"),
    (403, "PERMISSION_DENIED"),
    (404, "NOT_FOUND"),
])
def test_gemini_complete_does_not_retry_non_retryable_4xx(monkeypatch, status_code, status_name):
    """Deterministic client errors (4xx other than 429) must never be retried."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    call_log = _install_fake_gemini_sdk_with_failures(
        monkeypatch,
        failures=[_FakeClientError(code=status_code, status=status_name)],
    )

    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, max_retries=3)
    with pytest.raises(_FakeClientError):
        provider.complete(purpose="draft_claim", prompt="irrelevant")

    assert call_log["count"] == 1  # only initial attempt, 0 retries
    assert sleep_calls == []
    assert provider.last_provider_status == "provider_error"
    assert provider.last_retry_count == 0
    assert provider.last_rate_limit_events == []
    assert provider.last_call_finish_reason is None
    assert provider.last_token_usage is None
    assert provider.last_output_text_length is None


def test_final_provider_error_observability_is_internally_consistent(monkeypatch):
    """Regression test for the CE-02 smoke-run failure:
    When a successful call (e.g. CE-01) is followed by a call that fails with
    a ServerError (e.g. CE-02), the provider's observability attributes must NOT
    retain the previous call's provider_status='ok', finish_reason='STOP', or
    output_text_length."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    clock, fake_sleep, _ = _fake_clock_and_sleep()

    # Call 1: succeeds normally
    _install_fake_gemini_sdk_with_failures(monkeypatch, failures=[], text="claim text 12345")
    provider = GeminiClaimDraftProvider(sleep_fn=fake_sleep, clock_fn=clock, min_call_interval_seconds=0)
    provider.complete(purpose="draft_claim", prompt="first prompt")

    assert provider.last_provider_status == "ok"
    assert provider.last_call_finish_reason == "STOP"
    assert provider.last_output_text_length == len("claim text 12345")

    # Call 2: fails with 503 and exhausts retries (max_retries=1)
    provider.max_retries = 1
    _install_fake_gemini_sdk_with_failures(
        monkeypatch,
        failures=[_FakeServerError(503), _FakeServerError(503)],
    )

    with pytest.raises(_FakeServerError):
        provider.complete(purpose="draft_claim", prompt="second prompt")

    assert provider.last_provider_status == "provider_error"
    assert provider.last_call_finish_reason is None
    assert provider.last_output_text_length is None
    assert provider.last_token_usage is None
    assert provider.last_retry_count == 1
    assert len(provider.last_rate_limit_events) == 1

    # Observability helper check
    hybrid = HybridDraftClaimProvider(real_provider=provider)
    obs = _provider_observability(hybrid)
    assert obs["provider_status"] == "provider_error"
    assert obs["finish_reason"] is None
    assert obs["output_text_length"] is None
    assert obs["token_usage"] is None
    assert obs["retry_count"] == 1


def test_write_provider_error_task_result_sanitizes_stale_observability(tmp_path, suite):
    """Even if an observability dictionary containing stale successful-call metadata
    is passed to _write_provider_error_task_result, it is defensively sanitized to
    ensure finish_reason is null and provider_status is 'provider_error'."""
    task = next(t for t in suite.tasks if t.task_id == "CE-02")
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    stale_obs = {
        "finish_reason": "STOP",
        "latency_ms": 2549,
        "token_usage": {"prompt_token_count": 15, "total_token_count": 345},
        "output_text_length": 1585,
        "retry_count": 2,
        "rate_limit_events": [{"attempt": 1, "status": "UNAVAILABLE"}],
        "provider_status": "ok",
    }
    _write_provider_error_task_result(
        tasks_dir, task, _FakeServerError(503, "UNAVAILABLE"), stale_obs
    )

    saved = json.loads((tasks_dir / "CE-02.json").read_text())
    assert saved["status"] == "provider_error"
    assert saved["error_type"] == "_FakeServerError"
    assert saved["observability"]["provider_status"] == "provider_error"
    assert saved["observability"]["finish_reason"] is None
    assert saved["observability"]["token_usage"] is None
    assert saved["observability"]["output_text_length"] is None
    assert saved["observability"]["retry_count"] == 2
    assert len(saved["observability"]["rate_limit_events"]) == 1


def test_exhausted_503_remains_infrastructure_failure_and_not_graded(suite, subset, tmp_path):
    """When a real provider exhausts 503 retries during live_run, the task is marked
    as provider_error (an infrastructure failure), has no score/result, and is counted
    under provider_error_attempts, never graded as a model-quality failure."""
    class _503ExhaustedProvider(ModelProvider):
        provider_id = "503-test-provider"
        model_id = "fake-model"
        provider_class = ProviderClass.EXTERNAL_STANDARD
        allowed_data_classes = frozenset({Classification.PUBLIC})
        external_network_exposure = True

        def __init__(self):
            self.call_count = 0
            self.last_provider_status = "provider_error"
            self.last_retry_count = 2
            self.last_rate_limit_events = [{"attempt": 1, "status": "UNAVAILABLE"}]
            self.last_call_finish_reason = None
            self.last_token_usage = None
            self.last_output_text_length = None

        def credentials_present(self):
            return True

        def complete(self, purpose, prompt):
            self.call_count += 1
            raise _FakeServerError(503, "UNAVAILABLE")

    provider = _503ExhaustedProvider()
    results_dir = tmp_path / "real_model_results"
    outcome = live_run(
        suite=suite, subset=subset, results_dir=results_dir,
        configurations=("F",), repeat_count=1, real_provider=provider,
    )

    assert provider.call_count > 0
    assert outcome["manifest"]["provider_error_attempts"] > 0
    assert outcome["manifest"]["rate_limited_attempts"] == 0
    assert outcome["manifest"]["incomplete_attempts"] == 0

    tasks_dir = Path(outcome["run_dir"]) / "F" / "repeat_00" / "tasks"
    payloads = [json.loads(p.read_text()) for p in tasks_dir.glob("*.json")]
    failed = [p for p in payloads if p.get("status") == "provider_error"]
    assert failed
    assert all("score" not in p for p in failed)
    assert all("result" not in p for p in failed)
    assert failed[0]["error_type"] == "_FakeServerError"
    assert failed[0]["observability"]["provider_status"] == "provider_error"
    assert failed[0]["observability"]["finish_reason"] is None


def test_transient_server_error_classification_duck_typing():
    """Validates duck-typed classification of rate-limits, transient server errors,
    and non-retryable client errors without requiring google-genai package."""
    from afra.providers.real_model import (
        _is_rate_limit_error,
        _is_retryable_provider_error,
        _is_transient_server_error,
    )

    class CustomServerError(Exception):
        pass

    # 429 rate limits
    assert _is_rate_limit_error(_FakeRateLimitError())
    assert not _is_transient_server_error(_FakeRateLimitError())
    assert _is_retryable_provider_error(_FakeRateLimitError())

    # 5xx transient server errors
    for code in (500, 502, 503, 504):
        err = _FakeServerError(code=code)
        assert not _is_rate_limit_error(err)
        assert _is_transient_server_error(err)
        assert _is_retryable_provider_error(err)

    # gRPC status names
    class GrpcStatusError(Exception):
        def __init__(self, status):
            self.status = status

    assert _is_transient_server_error(GrpcStatusError("UNAVAILABLE"))
    assert _is_transient_server_error(GrpcStatusError("INTERNAL"))
    assert _is_retryable_provider_error(GrpcStatusError("UNAVAILABLE"))

    # ServerError class name
    assert _is_transient_server_error(CustomServerError("server down"))
    assert _is_retryable_provider_error(CustomServerError("server down"))

    # Non-retryable 4xx client errors
    for code in (400, 401, 403, 404, 409):
        err = _FakeClientError(code=code)
        assert not _is_rate_limit_error(err)
        assert not _is_transient_server_error(err)
        assert not _is_retryable_provider_error(err)

    assert not _is_transient_server_error(GrpcStatusError("INVALID_ARGUMENT"))
    assert not _is_retryable_provider_error(GrpcStatusError("INVALID_ARGUMENT"))

    # Unrelated standard exceptions
    assert not _is_retryable_provider_error(ValueError("local error"))
    assert not _is_retryable_provider_error(KeyError("missing key"))


# ---------------------------------------------------------------------------
# Antigravity CLI Provider Tests
# ---------------------------------------------------------------------------


def test_build_real_provider_factory():
    """Validates the provider factory returns the expected adapter and defaults."""
    assert SUPPORTED_REAL_PROVIDERS == ("gemini", "antigravity")

    # Gemini default
    gemini_p = build_real_provider("gemini")
    assert isinstance(gemini_p, GeminiClaimDraftProvider)
    assert gemini_p.model_id == "gemini-2.5-flash"

    # Gemini custom model
    gemini_custom = build_real_provider("gemini", model_id="gemini-custom")
    assert gemini_custom.model_id == "gemini-custom"

    # Antigravity default: points to actual model slug gemini-3.8-flash-low
    anti_p = build_real_provider("antigravity")
    assert isinstance(anti_p, AntigravityClaimDraftProvider)
    assert anti_p.model_id == DEFAULT_ANTIGRAVITY_MODEL_ID
    assert anti_p.model_id == "gemini-3.8-flash-low"
    assert anti_p.provider_id == "antigravity-cli-draft-claim-v1"

    # Antigravity custom model slug and case-insensitivity
    anti_custom = build_real_provider("ANTIGRAVITY", model_id="claude-sonnet-4-6")
    assert isinstance(anti_custom, AntigravityClaimDraftProvider)
    assert anti_custom.model_id == "claude-sonnet-4-6"

    # Unsupported provider raises ValueError
    with pytest.raises(ValueError, match="Unknown real-model provider"):
        build_real_provider("unsupported-backend")


def test_dry_run_with_antigravity_cli_provider(suite, subset):
    """dry_run with AntigravityClaimDraftProvider validates without process calls
    and accurately warns when agy CLI session is unavailable."""
    provider = AntigravityClaimDraftProvider(credentials_checker=lambda: False)
    report = dry_run(suite=suite, subset=subset, provider=provider)

    assert report.task_count == 14
    assert report.manifest["provider_id"] == "antigravity-cli-draft-claim-v1"
    assert report.manifest["model_id"] == "gemini-3.8-flash-low"
    assert not report.manifest["credentials_present"]
    assert any("AntigravityClaimDraftProvider is unavailable ('agy' CLI binary missing or unauthenticated)" in w for w in report.warnings)


def test_antigravity_cli_credentials_presence(monkeypatch):
    """Antigravity checks agy binary presence and authenticated session."""
    import shutil
    import subprocess

    # Case 1: Binary not in PATH
    monkeypatch.setattr(shutil, "which", lambda _: None)
    p1 = AntigravityClaimDraftProvider()
    assert not p1.credentials_present()

    with pytest.raises(RealModelCredentialsMissingError, match="missing or unauthenticated"):
        p1.complete(purpose="draft_claim", prompt="irrelevant")

    # Case 2: Binary present but session check fails (exit code 1)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/agy")

    def _mock_unauth_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="Error: please run `agy` to authenticate.\n",
        )

    p2 = AntigravityClaimDraftProvider(subprocess_runner=_mock_unauth_runner)
    assert not p2.credentials_present()

    # Case 3: Binary present and session check succeeds (exit code 0 and status SUCCESS)
    def _mock_auth_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "response": "gemini-3.8-flash-low"}),
            stderr="",
        )

    p3 = AntigravityClaimDraftProvider(subprocess_runner=_mock_auth_runner)
    assert p3.credentials_present()


def test_antigravity_cli_successful_structured_response():
    """Successful complete() call captures token usage, latency, finish_reason,
    and sets provider_status='ok' from agy JSON output."""
    import subprocess

    clock, fake_sleep, _ = _fake_clock_and_sleep()
    commands_run = []

    def _mock_runner(cmd, **kwargs):
        commands_run.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "SUCCESS",
                    "response": "Audited financial claim text from CLI.",
                    "duration_seconds": 1.45,
                    "usage": {
                        "input_tokens": 16,
                        "output_tokens": 9,
                        "thinking_tokens": 2,
                        "total_tokens": 27,
                    },
                }
            ),
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=0,
    )
    res = provider.complete(purpose="draft_claim", prompt="Summarize Contoso revenue")

    assert len(commands_run) == 1
    cmd = commands_run[0]
    assert cmd[0] == "agy"
    assert "-p" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert "--model" in cmd and "gemini-3.8-flash-low" in cmd
    assert "--disable-slash-commands" in cmd

    assert res.text == "Audited financial claim text from CLI."
    assert res.tokens_in == 16
    assert res.tokens_out == 9
    assert res.latency_ms >= 0

    assert provider.last_provider_status == "ok"
    assert provider.last_call_finish_reason == "STOP"
    assert provider.last_finish_reason == "STOP"
    assert provider.last_retry_count == 0
    assert provider.last_token_usage == {
        "prompt_token_count": 16,
        "candidates_token_count": 9,
        "thoughts_token_count": 2,
        "total_token_count": 27,
    }
    assert provider.last_output_text_length == len("Audited financial claim text from CLI.")


def test_antigravity_cli_selected_model_propagation():
    """Validates that custom model slugs (e.g. claude-sonnet-4-6) are correctly passed to the agy command."""
    import subprocess

    commands_run = []

    def _mock_runner(cmd, **kwargs):
        commands_run.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "response": "Claim text"}),
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        model_id="claude-sonnet-4-6",
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=0,
    )
    provider.complete(purpose="draft_claim", prompt="prompt")

    assert provider.model_id == "claude-sonnet-4-6"
    assert len(commands_run) == 1
    cmd = commands_run[0]
    model_idx = cmd.index("--model")
    assert cmd[model_idx + 1] == "claude-sonnet-4-6"


def test_antigravity_cli_only_permits_draft_claim_purpose():
    """Like GeminiClaimDraftProvider, AntigravityClaimDraftProvider strictly rejects non-draft_claim purposes."""
    provider = AntigravityClaimDraftProvider(credentials_checker=lambda: True)

    for invalid in ("plan_research", "extract_claims", "review_claim", "decide_publication"):
        with pytest.raises(UnsupportedPurposeError, match="draft_claim"):
            provider.complete(purpose=invalid, prompt="irrelevant")


def test_antigravity_cli_policy_boundary():
    """Declares EXTERNAL_STANDARD / PUBLIC-only / network-exposed policy properties identically to Gemini."""
    provider = AntigravityClaimDraftProvider(credentials_checker=lambda: True)
    assert provider.provider_class == ProviderClass.EXTERNAL_STANDARD
    assert provider.allowed_data_classes == frozenset({Classification.PUBLIC})
    assert provider.external_network_exposure is True

    # Policy enforcement blocks non-public data
    decision = enforce_model_call_policy(
        classification=Classification.INTERNAL,
        prompt="internal prompt",
        requested_provider_class=ProviderClass.EXTERNAL_STANDARD,
        providers={ProviderClass.EXTERNAL_STANDARD: provider},
    )
    assert decision.allowed is False
    assert decision.action == DLPAction.BLOCK


def test_antigravity_cli_retries_quota_exceeded_then_succeeds():
    """Transient quota/rate-limit failure from CLI is retried with backoff and recovers."""
    import subprocess

    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    call_count = {"count": 0}

    def _mock_runner(cmd, **kwargs):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="Error: 429 RESOURCE_EXHAUSTED: quota exceeded for model\n",
            )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "response": "Recovered claim after quota retry."}),
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=1.0,
        max_retries=3,
    )
    res = provider.complete(purpose="draft_claim", prompt="prompt")

    assert call_count["count"] == 2
    assert res.text == "Recovered claim after quota retry."
    assert provider.last_retry_count == 1
    assert provider.last_provider_status == "ok"
    assert len(provider.last_rate_limit_events) == 1
    assert provider.last_rate_limit_events[0]["status"] == "RESOURCE_EXHAUSTED"
    assert len(sleep_calls) == 1


def test_antigravity_cli_exhausted_quota_raises_rate_limit_exhausted():
    """When CLI quota retries are exhausted, raises RateLimitExhaustedError and cleans state."""
    import subprocess

    clock, fake_sleep, _ = _fake_clock_and_sleep()

    def _mock_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="Error: quota exhausted. Please try again later.\n",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=0,
        max_retries=2,
    )

    with pytest.raises(RateLimitExhaustedError) as excinfo:
        provider.complete(purpose="draft_claim", prompt="prompt")

    assert excinfo.value.retry_count == 2
    assert provider.last_provider_status == "rate_limited"
    assert provider.last_retry_count == 2
    assert provider.last_call_finish_reason is None
    assert provider.last_token_usage is None
    assert provider.last_output_text_length is None


def test_antigravity_cli_retries_transient_server_overload():
    """Transient server overload (503 / high demand) is retried with backoff and succeeds."""
    import subprocess

    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    attempts = {"count": 0}

    def _mock_runner(cmd, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="503 UNAVAILABLE: Model is currently experiencing high demand. Please try again later.\n",
            )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "response": "Recovered from 503."}),
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=1.0,
        max_retries=3,
    )
    res = provider.complete(purpose="draft_claim", prompt="prompt")

    assert attempts["count"] == 2
    assert res.text == "Recovered from 503."
    assert provider.last_retry_count == 1
    assert provider.last_provider_status == "ok"
    assert len(sleep_calls) == 1


def test_antigravity_cli_timeout_retries_and_exhaustion():
    """CLI subprocess timeouts are retried, and raise AntigravityCliError when retries are exhausted."""
    import subprocess

    clock, fake_sleep, _ = _fake_clock_and_sleep()
    attempts = {"count": 0}

    def _mock_runner(cmd, **kwargs):
        attempts["count"] += 1
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 60))

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=0,
        max_retries=2,
    )

    with pytest.raises(AntigravityCliError, match="timed out"):
        provider.complete(purpose="draft_claim", prompt="prompt")

    assert attempts["count"] == 3  # Initial attempt + 2 retries
    assert provider.last_provider_status == "provider_error"
    assert provider.last_retry_count == 2
    assert provider.last_call_finish_reason is None
    assert provider.last_token_usage is None


def test_antigravity_cli_non_retryable_auth_failure_fails_fast():
    """Unauthenticated CLI session fails immediately on attempt 0 without retrying."""
    import subprocess

    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    attempts = {"count": 0}

    def _mock_runner(cmd, **kwargs):
        attempts["count"] += 1
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="Error: unauthenticated. Please run `agy` to sign in.\n",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,  # Bypass pre-check to test runner classification
        min_call_interval_seconds=1.0,
        max_retries=3,
    )

    with pytest.raises(AntigravityCliError, match="unauthenticated"):
        provider.complete(purpose="draft_claim", prompt="prompt")

    assert attempts["count"] == 1
    assert provider.last_retry_count == 0
    assert provider.last_provider_status == "provider_error"
    assert sleep_calls == []


def test_antigravity_cli_malformed_json_fails_fast():
    """Malformed non-JSON output from CLI raises AntigravityCliError immediately without retrying."""
    import subprocess

    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    attempts = {"count": 0}

    def _mock_runner(cmd, **kwargs):
        attempts["count"] += 1
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="fatal panic: nil pointer dereference at runtime\n",
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=1.0,
        max_retries=3,
    )

    with pytest.raises(AntigravityCliError, match="malformed JSON"):
        provider.complete(purpose="draft_claim", prompt="prompt")

    assert attempts["count"] == 1
    assert provider.last_retry_count == 0
    assert provider.last_provider_status == "provider_error"
    assert sleep_calls == []


def test_antigravity_cli_incomplete_completion_on_non_stop():
    """A non-STOP finish_reason from the CLI raises IncompleteCompletionError."""
    import subprocess

    clock, fake_sleep, _ = _fake_clock_and_sleep()

    def _mock_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "SUCCESS",
                    "finish_reason": "MAX_TOKENS",
                    "response": "Truncated claim text",
                }
            ),
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=0,
    )

    with pytest.raises(IncompleteCompletionError) as excinfo:
        provider.complete(purpose="draft_claim", prompt="prompt")

    assert excinfo.value.finish_reason == "MAX_TOKENS"
    assert excinfo.value.partial_text == "Truncated claim text"
    assert provider.last_provider_status == "incomplete"
    assert provider.last_call_finish_reason == "MAX_TOKENS"


def test_antigravity_cli_infrastructure_failure_separation(suite, subset, tmp_path):
    """When the Antigravity CLI encounters an infrastructure failure during live_run,
    the task is marked as provider_error, counted in provider_error_attempts, and never graded."""
    import subprocess

    clock, fake_sleep, _ = _fake_clock_and_sleep()

    def _failing_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="fatal: network pipe closed\n",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_failing_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=0,
    )
    results_dir = tmp_path / "antigravity_infra_fail"

    outcome = live_run(
        suite=suite,
        subset=subset,
        results_dir=results_dir,
        configurations=("F",),
        repeat_count=1,
        real_provider=provider,
    )

    assert outcome["manifest"]["provider_error_attempts"] > 0
    assert outcome["manifest"]["rate_limited_attempts"] == 0
    assert outcome["manifest"]["incomplete_attempts"] == 0

    tasks_dir = Path(outcome["run_dir"]) / "F" / "repeat_00" / "tasks"
    payloads = [json.loads(p.read_text()) for p in tasks_dir.glob("*.json")]
    failed = [p for p in payloads if p.get("status") == "provider_error"]
    assert failed
    assert all("score" not in p for p in failed)
    assert failed[0]["error_type"] == "AntigravityCliError"
    assert failed[0]["observability"]["provider_status"] == "provider_error"


def test_antigravity_cli_in_live_run_smoke_test(suite, subset, tmp_path):
    """Integration test: AntigravityClaimDraftProvider in live_run executes
    Configuration F across the frozen subset, producing scored results and valid manifest."""
    import subprocess

    clock, fake_sleep, _ = _fake_clock_and_sleep()

    def _mock_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "SUCCESS",
                    "response": "Verified audited financial research finding.",
                    "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                }
            ),
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=0,
    )
    results_dir = tmp_path / "antigravity_cli_results"

    outcome = live_run(
        suite=suite,
        subset=subset,
        results_dir=results_dir,
        configurations=("F",),
        repeat_count=1,
        real_provider=provider,
    )

    assert outcome["manifest"]["provider_id"] == "hybrid[antigravity-cli-draft-claim-v1+test-double-external]"
    assert outcome["manifest"]["model_id"] == DEFAULT_ANTIGRAVITY_MODEL_ID
    assert outcome["manifest"]["model_id"] == "gemini-3.8-flash-low"
    assert outcome["manifest"]["incomplete_attempts"] == 0
    assert outcome["manifest"]["rate_limited_attempts"] == 0
    assert outcome["manifest"]["provider_error_attempts"] == 0

    run_dir = Path(outcome["run_dir"])
    manifest_file = run_dir / "manifest.json"
    assert manifest_file.exists()

    aggregate_file = run_dir / "F" / "repeat_00" / "aggregate.json"
    assert aggregate_file.exists()
    agg_data = json.loads(aggregate_file.read_text())
    assert agg_data["task_count"] == 14
    assert agg_data["completion_rate"]["n"] == 14

    task_files = list((run_dir / "F" / "repeat_00" / "tasks").glob("*.json"))
    assert len(task_files) == 14
    assert all("score" in json.loads(p.read_text()) for p in task_files)


def test_antigravity_cli_dns_failure_then_success():
    """Transient DNS lookup failure is classified as network_error, retried with backoff, and succeeds."""
    import subprocess

    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    attempts = {"count": 0}

    def _mock_runner(cmd, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="dial tcp: lookup daily-cloudcode-pa.googleapis.com: no such host\n",
            )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "response": "Recovered claim after DNS resolution failure."}),
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=1.0,
        max_retries=3,
    )
    res = provider.complete(purpose="draft_claim", prompt="prompt")

    assert attempts["count"] == 2
    assert res.text == "Recovered claim after DNS resolution failure."
    assert provider.last_retry_count == 1
    assert provider.last_provider_status == "ok"
    assert len(provider.last_rate_limit_events) == 1
    assert provider.last_rate_limit_events[0]["status"] == "NETWORK_ERROR"
    assert provider.last_rate_limit_events[0]["source"] == "backoff"
    assert len(sleep_calls) == 1


def test_antigravity_cli_repeated_dns_failure_exhausts_retries():
    """Repeated DNS failures exhaust retries, raise AntigravityCliError, and mark provider_error."""
    import subprocess

    clock, fake_sleep, _ = _fake_clock_and_sleep()
    attempts = {"count": 0}

    def _mock_runner(cmd, **kwargs):
        attempts["count"] += 1
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="dial tcp: lookup daily-cloudcode-pa.googleapis.com: no such host\n",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=0,
        max_retries=2,
    )

    with pytest.raises(AntigravityCliError, match="network_error.*no such host"):
        provider.complete(purpose="draft_claim", prompt="prompt")

    assert attempts["count"] == 3  # Initial attempt + 2 retries
    assert provider.last_provider_status == "provider_error"
    assert provider.last_retry_count == 2
    assert len(provider.last_rate_limit_events) == 2
    assert provider.last_rate_limit_events[0]["status"] == "NETWORK_ERROR"
    assert provider.last_rate_limit_events[1]["status"] == "NETWORK_ERROR"
    assert provider.last_call_finish_reason is None
    assert provider.last_token_usage is None
    assert provider.last_output_text_length is None


def test_antigravity_cli_timeout_then_success():
    """Subprocess TimeoutExpired on first attempt is retried and succeeds on second attempt."""
    import subprocess

    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    attempts = {"count": 0}

    def _mock_runner(cmd, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 60))
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "response": "Recovered claim after timeout."}),
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=1.0,
        max_retries=3,
    )
    res = provider.complete(purpose="draft_claim", prompt="prompt")

    assert attempts["count"] == 2
    assert res.text == "Recovered claim after timeout."
    assert provider.last_retry_count == 1
    assert provider.last_provider_status == "ok"
    assert len(provider.last_rate_limit_events) == 1
    assert provider.last_rate_limit_events[0]["status"] == "TIMEOUT"
    assert len(sleep_calls) == 1


def test_antigravity_cli_connection_reset_then_success():
    """Transient TCP connection reset is classified as network_error, retried, and succeeds."""
    import subprocess

    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    attempts = {"count": 0}

    def _mock_runner(cmd, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="read tcp 127.0.0.1:54321->142.250.180.10:443: read: connection reset by peer\n",
            )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "response": "Recovered claim after connection reset."}),
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=1.0,
        max_retries=3,
    )
    res = provider.complete(purpose="draft_claim", prompt="prompt")

    assert attempts["count"] == 2
    assert res.text == "Recovered claim after connection reset."
    assert provider.last_retry_count == 1
    assert provider.last_provider_status == "ok"
    assert len(provider.last_rate_limit_events) == 1
    assert provider.last_rate_limit_events[0]["status"] == "NETWORK_ERROR"
    assert len(sleep_calls) == 1


def test_antigravity_cli_auth_and_invalid_model_errors_fail_fast():
    """Auth errors and invalid model errors fail immediately on attempt 0 without retries or sleeps."""
    import subprocess

    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()

    # 1. Auth error (session expired)
    auth_attempts = {"count": 0}

    def _auth_runner(cmd, **kwargs):
        auth_attempts["count"] += 1
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="Error: session expired. Please sign in with `agy login`.\n",
        )

    auth_provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_auth_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=10.0,
        max_retries=3,
    )

    with pytest.raises(AntigravityCliError, match="session expired"):
        auth_provider.complete(purpose="draft_claim", prompt="prompt")

    assert auth_attempts["count"] == 1
    assert auth_provider.last_retry_count == 0
    assert auth_provider.last_rate_limit_events == []
    assert sleep_calls == []

    # 2. Invalid model error
    model_attempts = {"count": 0}

    def _model_runner(cmd, **kwargs):
        model_attempts["count"] += 1
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="Error: unknown model 'nonexistent-model-slug'. Run `agy models` to see available models.\n",
        )

    model_provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_model_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=10.0,
        max_retries=3,
    )

    with pytest.raises(AntigravityCliError, match="unknown model"):
        model_provider.complete(purpose="draft_claim", prompt="prompt")

    assert model_attempts["count"] == 1
    assert model_provider.last_retry_count == 0
    assert model_provider.last_rate_limit_events == []
    assert sleep_calls == []


def test_antigravity_cli_pacing_applied_on_all_network_retries():
    """Network retries and successive calls strictly enforce min_call_interval_seconds pacing."""
    import subprocess

    clock, fake_sleep, sleep_calls = _fake_clock_and_sleep()
    attempts = {"count": 0}

    def _mock_runner(cmd, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="dial tcp: lookup daily-cloudcode-pa.googleapis.com: no such host\n",
            )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "response": "Paced response."}),
            stderr="",
        )

    provider = AntigravityClaimDraftProvider(
        sleep_fn=fake_sleep,
        clock_fn=clock,
        subprocess_runner=_mock_runner,
        credentials_checker=lambda: True,
        min_call_interval_seconds=15.0,
        max_retries=3,
    )

    # First call: attempt 1 fails with DNS error, retry 1 must sleep for at least 15.0s pacing floor
    res1 = provider.complete(purpose="draft_claim", prompt="first prompt")
    assert res1.text == "Paced response."
    assert attempts["count"] == 2
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 15.0  # max(10.0 * 2^0, 15.0) = 15.0

    # Second call immediately after: inter-call pacing must enforce 15.0s since last call finished
    res2 = provider.complete(purpose="draft_claim", prompt="second prompt")
    assert res2.text == "Paced response."
    assert attempts["count"] == 3
    assert len(sleep_calls) == 2
    assert sleep_calls[1] == 15.0
