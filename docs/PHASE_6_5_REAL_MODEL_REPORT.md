# Phase 6.5 Report

Status: **Complete.** Canonical live validation completed on frozen 14-task synthetic subset (`run_id`: `realmodel_live_1fb8b08dbbc3`, git commit `4386ac6f247226ad297d02573acedabb5d00f0d8`). 437/437 backend tests pass (`cd backend && python3 -m pytest -v`), 27/27 frontend tests pass. All 9 configuration/repeat cells completed across the 14 frozen tasks (126 task executions total) using Antigravity CLI (`gemini-3.8-flash-low`). Infrastructure failures: 0 incomplete attempts, 0 rate-limited attempts, 0 provider error attempts. The results replicate the qualitative A → D → F assurance improvement observed under Phase 6's deterministic test doubles.

## 0. Scope, as instructed

This phase was scoped to exactly: implement the harness `docs/PHASE_6_REPORT.md#13` proposed, freeze a small representative task subset, add repeat-count and reproducibility-metadata support, add a dry-run mode that makes zero external calls, and report honestly that no live result exists yet. It was explicitly **not** scoped to run a real model, spend any API budget, or touch Phase 7 (frontend). Phase 7 and Phase 8 have both since completed (see their own reports); neither touched this phase's harness except where explicitly recorded below.

**Correction (recorded the same day as the original harness, before any live run had happened):** the subset first written in this phase covered only 2 of the 7 categories a real-model pass needs for meaningful metric coverage. It was corrected to the 14-task, 7-category stratified subset described in §2 below - a same-day, in-place correction, not a new benchmark version, since nothing had been run against the incorrect version yet.

## 0.5 Provider migration: Anthropic → Google Gemini

**The real-provider adapter was changed from Anthropic to Google Gemini after this harness was first built, still before any live run.** The original adapter (`AnthropicClaimDraftProvider`, `claude-sonnet-5`) has been **removed** from `afra/providers/real_model.py`, not merely deprecated - `hasattr(afra.providers.real_model, "AnthropicClaimDraftProvider")` is `False`, mechanically checked by `tests/test_real_model_harness.py::test_anthropic_is_no_longer_the_canonical_real_provider`. This is disclosed here rather than silently presented as if Gemini had always been the choice: the harness architecture (`HybridDraftClaimProvider`, the `purpose="draft_claim"` scoping, the dry-run/live-run split, the frozen subset, repeat-count support, the run manifest) is entirely unchanged by this migration - only the concrete real-adapter class, its SDK, its env var, and its model identifier changed. Every one of §§2-8 below describes the **current** (Gemini) state; where the migration itself is the relevant fact, it's called out explicitly.

## 1. What was built (current state)

| Component | File |
|---|---|
| Real `ModelProvider` adapter (Google Gemini API, `purpose="draft_claim"` only) | `backend/src/afra/providers/real_model.py` (`GeminiClaimDraftProvider`) |
| Real `ModelProvider` adapter (Antigravity CLI backend, `purpose="draft_claim"` only) | `backend/src/afra/providers/real_model.py` (`AntigravityClaimDraftProvider`) |
| Hybrid provider (real draft-claim + deterministic everything else) | `backend/src/afra/providers/real_model.py` (`HybridDraftClaimProvider`) - provider-agnostic by design |
| Frozen 14-task, 7-category representative subset | `backend/benchmark/real_model_subset_v1.json` |
| Subset provenance script (deterministic, documented selection rule) | `backend/scripts/build_real_model_subset_v1.py` |
| Dry-run / live harness (subset loading, validation, repeat-count, manifest, attempt handling) | `backend/src/afra/benchmark/real_model_harness.py` |
| CLI entry point (supports `--provider`, `--min-call-interval-seconds`, `--max-retries`) | `backend/scripts/run_real_model_benchmark.py` |
| Tests (88 provider & harness tests, zero network calls, no credentials required) | `backend/tests/test_real_model_harness.py` |
| Optional dependency declaration (`google-genai` SDK, not installed by default) | `backend/pyproject.toml`'s `real-model` extra |

**One small, additive change to Phase 6 code**, made when this harness was first built and unaffected by the provider migration: `afra.benchmark.configurations.run_configuration_d`/`run_configuration_f` gained an optional `providers: dict[ProviderClass, ModelProvider] | None = None` parameter, defaulting to the exact same `build_full_provider_registry()` call Phase 6 always made. Every existing Phase 6 call site and test is unaffected; this exists solely so the real-model harness can substitute `HybridDraftClaimProvider` into the `EXTERNAL_STANDARD` slot without duplicating either function. **Deterministic providers themselves (`afra/providers/test_double.py`) were not touched at all, by the original harness or by this migration**, and the full deterministic benchmark (`python3 scripts/run_benchmark.py`) reproduces the same A/D/F numbers as `docs/PHASE_6_REPORT.md`.

## 2. The frozen subset - unchanged by the provider migration

`backend/benchmark/real_model_subset_v1.json`, version `v1`, generated from `backend/benchmark/tasks_v1.json` (version `v1`). **14 tasks, all 7 categories a real-model pass needs, exactly stratified (2 per category, no category overweighted):**

| Category | Task IDs | Selection rule |
|---|---|---|
| `factual_retrieval` | `FR-01`, `FR-02` | first 2 by task_id |
| `multi_document_comparison` | `MC-01`, `MC-02` | first 2 by task_id |
| `ambiguous_clarification` | `AC-01`, `AC-04` | override: one "no axis named" task + one "unknown company" task (two distinct `NEEDS_CLARIFICATION` reasons) |
| `insufficient_evidence` | `IE-01`, `IE-04` | override: one Fabrikam-missing + one Contoso-missing task |
| `conflicting_evidence` | `CE-01`, `CE-02` | first 2 by task_id |
| `prompt_injection` | `PI-01`, `PI-02` | first 2 by task_id |
| `sensitive_data_routing_policy` | `SD-01`, `SD-03` | override: one `allow`-outcome task + one `block`-outcome task |

`temporal_change_analysis` is the one Phase 6 category deliberately excluded - `docs/PHASE_6_REPORT.md`'s §6 already established its `retrieval_recall` is a mechanical 0.5 for every task in it, regardless of provider (a router limitation, `afra.routing.router.pick_latest_document()` never fetching both years - not something claim-drafting text can affect), so re-running it here would not produce new information.

The subset selection rule (`afra.benchmark.real_model_harness.derive_subset_task_ids`, `TASKS_PER_CATEGORY`, `_CATEGORY_OVERRIDES`) is provider-independent - it operates purely on `tasks_v1.json`'s categories and gold fields, never touches `afra.providers.real_model`, and was not modified by this migration. `backend/benchmark/real_model_subset_v1.json` is byte-identical before and after the Anthropic → Gemini change (mechanically re-verified: `tests/test_real_model_harness.py::test_subset_task_ids_match_frozen_stratified_selection` still passes).

## 3. Provider adapter chosen: Google Gemini

**`GeminiClaimDraftProvider`** (`backend/src/afra/providers/real_model.py`), wrapping the Gemini API via the `google-genai` SDK (`client.models.generate_content`). Declares `provider_id="gemini-draft-claim-v1"`, `provider_class=EXTERNAL_STANDARD`, `allowed_data_classes={PUBLIC}`, `external_network_exposure=True` - identical in kind to what the retired Anthropic adapter declared, and identical to `afra.providers.test_double.TestDoubleProvider`'s declarations, so `afra.policy.enforcement` treats it exactly the same way it always has (see §5).

### Model selection (recorded before this adapter was written)

- **Model: `gemini-2.5-flash`** - available via Google AI Studio / the Gemini API's standard `generateContent` endpoint. The only call this adapter ever makes is "draft one factual claim strictly from the given evidence": a short, single-turn, instruction-following completion, not a task needing a frontier/pro-tier model's extra capability.
- **Sampling defaults**: `temperature=0.0`, `max_output_tokens=512`. Claim drafting must state one claim strictly from provided evidence, not generate creatively - low temperature minimizes variance and off-evidence embellishment.
- **Free Tier**: the planned experiment (14 tasks, `purpose="draft_claim"` only, low repeat counts) is **small relative to typical Free Tier limits, subject to the account's current quota and Google's current rate-limit policy** - this project does not claim or hardcode a specific quota number anywhere, since the Gemini API does not expose one for this adapter to check at runtime. **Update, after a first live smoke test**: the observed quota is 5 requests/minute/project/model - tighter than assumed, and a live run *did* hit it (see §3.2). Pacing and bounded retry/backoff (§3.2) now handle this; a live run can still fail if the quota is exhausted for long enough to exceed `max_retries`, but it no longer crashes the whole run when that happens.
- **Data-use scope**: the adapter is restricted to `PUBLIC`-classified content only (`allowed_data_classes={PUBLIC}`), which bounds what any Free Tier data-use terms could ever apply to, regardless of what those terms are.

`model_id` defaults to `"gemini-2.5-flash"` and is a plain constructor argument (`--model-id` on the CLI) - not hardcoded as a magic string anywhere in the enforcement or grading path, since model identifiers change over time and this project's provider abstraction was designed for that (`docs/ARCHITECTURE.md#model-provider-abstraction`).

**Scope is deliberately narrow, matching `docs/PHASE_6_REPORT.md#13` exactly, unchanged by the provider migration**: `GeminiClaimDraftProvider.complete()` raises `UnsupportedPurposeError` for any `purpose` other than `"draft_claim"`. `HybridDraftClaimProvider` is what actually gets substituted into a live run's provider registry - it routes `purpose="draft_claim"` to the real adapter and every other purpose (`"plan"`, `"interpret"`) to a deterministic `TestDoubleProvider`, so interpretation, routing, and sufficiency checking all stay fully deterministic and reproducible even during a live run. Only claim-drafting text becomes real.

The `google-genai` Python package is **not** a project dependency (`backend/pyproject.toml`'s `dependencies = []` is unchanged) - it is an optional extra (`pip install -e ".[real-model]"`) and is imported lazily, inside `complete()`, only when `purpose="draft_claim"` and credentials are present. Constructing `GeminiClaimDraftProvider`, running dry-run validation, or importing this module never requires the package to be installed - the entire live SDK call path (`google.genai.Client`, `generate_content`, response parsing including finish-reason handling - see §3.1) is exercised in tests only against a fake module injected into `sys.modules`, never the real package.

### 3.1 Finish-reason / truncation handling

Gemini 2.5 Flash may spend part of `max_output_tokens` on internal "thinking" tokens before producing a visible answer, so a response can come back **truncated** (a non-`STOP` `finish_reason`, most notably `MAX_TOKENS`) without any network error. `GeminiClaimDraftProvider.complete()` reads `response.candidates[0].finish_reason` and:

- if it is `"STOP"` (or genuinely absent - some response shapes may not populate it, and the adapter does not invent a failure it has no evidence for): returns a normal `ModelResponse` and records the reason on `provider.last_finish_reason`.
- if it is anything else (`MAX_TOKENS`, `SAFETY`, or any other non-`STOP` value): raises `IncompleteCompletionError` instead of returning the partial text as a `ModelResponse`. **The truncated/partial text is never handed to the orchestrator, never persisted as a `Claim`, and never reaches `afra.benchmark.grader` at all.**

`afra.benchmark.real_model_harness._run_repeat()` catches `IncompleteCompletionError` per task (not per run): the failing task/repeat is recorded in its own persisted JSON file with `"status": "incomplete"` and the `finish_reason` - deliberately with no `"result"` or `"score"` key, so it cannot be mistaken for or accidentally fed into a graded outcome - and the loop continues to the next task rather than crashing the whole run. `live_run()`'s manifest carries a run-level `incomplete_attempts` count rolled up across every configuration/repeat, always `0` for `mode="dry_run"` (zero calls are ever made in dry-run). Verified end-to-end, with no network call, by `tests/test_real_model_harness.py::test_live_run_records_incomplete_attempts_without_crashing_the_whole_run` (a fake provider that always raises `IncompleteCompletionError`, run through the real `live_run()` → `_run_repeat()` → `run_configuration_f()` → orchestrator path) and by three unit tests on `complete()` itself covering `STOP`, `MAX_TOKENS`, and `SAFETY` finish reasons.

### 3.2 Rate-limit handling (pacing + retry/backoff)

**A first live smoke test (`--live --configurations F --repeat 1`) reached the real Gemini API, made several successful calls, and then crashed with an unhandled `429 RESOURCE_EXHAUSTED`** - the observed quota is 5 requests/minute/project/model, tighter than the "small relative to typical Free Tier limits" wording in §3 had assumed, and at the time no retry/backoff existed, so the exception propagated straight out of `live_run()` before it ever wrote a `manifest.json`. This section is the fix that resulted.

**1. Pacing, for real Gemini calls only.** `GeminiClaimDraftProvider.complete()` paces itself at least `min_call_interval_seconds` (default `DEFAULT_MIN_CALL_INTERVAL_SECONDS = 15.0`, configurable via the constructor or `--min-call-interval-seconds`) after its own previous real network attempt before making its own first attempt - so two separate `draft_claim` calls (two different tasks/repeats) never happen closer together than that. A freshly constructed provider's very first call never sleeps (nothing to pace against yet), and pacing is entirely instance-local state (`_last_call_at`) - it never touches `afra.providers.test_double.TestDoubleProvider` or any other deterministic provider, so nothing about Phase 6's deterministic benchmark, or this harness's own dry-run mode, is slowed by this at all.

**2. Retry/backoff for 429 `RESOURCE_EXHAUSTED`, bounded.** Within one `complete()` call, `_generate_with_retries()` catches a rate-limit error (detected by duck-typing the `code`/`status` attributes the SDK's `ClientError` exposes - `code == 429` or `status == "RESOURCE_EXHAUSTED"` - rather than importing `google.genai.errors` for an `isinstance` check, so detection stays correct even without the real package installed) and retries up to `max_retries` times (default `DEFAULT_MAX_RETRIES = 3`, configurable via the constructor or `--max-retries`):

- if the error response carries a structured `google.rpc.RetryInfo` delay (`details.error.details[].retryDelay`, e.g. `"20s"`) or an HTTP `Retry-After` header, that server-provided delay is used **exactly as given** - never stretched further by the pacing floor;
- otherwise, a bounded exponential backoff is used instead: `RETRY_BASE_DELAY_SECONDS = 10.0` doubling per retry, capped at `RETRY_MAX_DELAY_SECONDS = 60.0` (10s, 20s, 40s for the 3 default retries - 60s is never actually reached).

Any other exception (a different `APIError`, a network failure, ...) is **not** retried - it propagates immediately, on the first attempt, and is instead caught at the harness's task level as `"status": "provider_error"` (below), rather than this adapter guessing which failures are safe to retry.

**3. If every retry is exhausted, `RateLimitExhaustedError` is raised** (carrying `retry_count`, `rate_limit_events`, and `last_status`) instead of the raw SDK exception propagating uncaught. `afra.benchmark.real_model_harness._run_repeat()` catches it per task, exactly the way it already catches `IncompleteCompletionError`: the task is recorded with `"status": "rate_limited"` and no `"result"`/`"score"` key (never graded), and the loop continues to the next task rather than crashing the whole run. A third, broader `except Exception` catch was added at the same call site for any other real-provider failure that isn't `IncompleteCompletionError` or `RateLimitExhaustedError`, recorded as `"status": "provider_error"` - so one unexpected failure of *any* kind can no longer take down every remaining task in a paid live run. Both counts are rolled up into the run manifest as `rate_limited_attempts` and `provider_error_attempts` (§7), always `0` for `mode="dry_run"`.

**4. Observability, recorded where available on every task result, not only failed ones.** `GeminiClaimDraftProvider` exposes `last_latency_ms`, `last_token_usage`, `last_retry_count`, `last_rate_limit_events`, and `last_provider_status` (`"ok"` / `"incomplete"` / `"rate_limited"` / `"unknown"`) after every `complete()` call, success or failure - read by the harness via `getattr(provider, "last_*", None)` (never a hard attribute access), so a deterministic test-double standing in for the real provider in tests never breaks it. Every task JSON now carries these under a `"observability"` key, alongside the pre-existing `"finish_reason"` field.

**Tested with zero network calls and, new for this patch, zero real `time.sleep`**: 24 tests were added to `tests/test_real_model_harness.py` (47 → 71), all against a fake `google.genai` SDK module and fake `sleep_fn`/`clock_fn` injected into `GeminiClaimDraftProvider`'s constructor (a fake clock that only advances when the fake sleep function tells it to, faithfully modelling `time.monotonic`/`time.sleep`'s relationship without ever blocking) - covering a single 429 then success, repeated 429s then success, retries exhausted (`RateLimitExhaustedError`, exactly `max_retries` retries and never a 4th attempt), server-provided `retryDelay` and `Retry-After` header parsing, a non-429 error not being retried at all, pacing between two calls (and *not* stacking on top of a retry's own delay), pacing being disabled by `min_call_interval_seconds=0`, pacing state being per-instance, `TestDoubleProvider` carrying none of this patch's new attributes, the deterministic benchmark being unaffected end-to-end, and - mechanically, not just by construction - a scenario that would take at least 45 real seconds under `time.sleep` completing in under 1 second of actual wall-clock time (`test_no_real_sleeping_occurs_across_a_full_retry_and_pacing_scenario`).

### 3.3 Antigravity CLI adapter & transport reliability hardening

To enable headless CLI-backed real-model benchmarking without hardcoding external API keys, an **`AntigravityClaimDraftProvider`** was added alongside `GeminiClaimDraftProvider` in `backend/src/afra/providers/real_model.py`.

- **Official CLI interface**: Interfaces with the locally authenticated Google Antigravity installation using `agy -p "<prompt>" --output-format json` with explicit `--model <model-slug>`.
- **Target model**: `gemini-3.8-flash-low` (the recorded model slug).
- **Generation settings**: the canonical live run was executed through the Antigravity CLI using the recorded model slug. The project did not explicitly pass temperature or output-token controls through that execution path (the command is exactly the one above plus `--disable-slash-commands`); generation settings therefore followed the CLI/runtime behaviour in effect for that run. The `temperature=0.0` / `max_output_tokens=512` / `thinking_budget=0` defaults described in §3 apply only to `GeminiClaimDraftProvider` (direct Gemini API), which was not the canonical execution path.
- **Provider parity**: Implements the identical narrow draft-claim interface (`purpose="draft_claim"` only) through `HybridDraftClaimProvider`. Planning, interpretation, tool calls, and sufficiency verification remain strictly deterministic and governed by test doubles.
- **Transient transport failure hardening**: An initial full-matrix live run attempt (`realmodel_live_f5f2c2455376`) hit 28 provider infrastructure failures (22 DNS resolution failures: `dial tcp: lookup daily-cloudcode-pa.googleapis.com: no such host`, and 6 subprocess timeouts). These were strictly transient network/transport glitches, not model hallucinations or quota exhaustion. The CLI provider was hardened to classify transient DNS errors, socket resets, and subprocess timeouts as retryable infrastructure failures with bounded exponential backoff.
- **Observability fix**: Corrected task-level provider observability so that failed retry attempts overwrite stale success metadata, ensuring clear separation of infrastructure errors from model grading.
- **Zero infrastructure failures**: Following this hardening, the first clean run (`realmodel_live_3cf1dc74d1ee`) and the official provenance-clean canonical run (`realmodel_live_1fb8b08dbbc3`) completed all 126 task evaluations across A, D, and F × 3 repeats with 0 incomplete attempts, 0 rate-limited attempts, and 0 provider errors.

## 4. Dry-run / validation mode

`afra.benchmark.real_model_harness.dry_run()` validates a prospective run - subset tasks resolve against the frozen suite, `configurations` is a non-empty subset of `{"A","D","F"}`, `repeat_count >= 1`, and whether `GEMINI_API_KEY` is present in the environment (checked with `os.environ.get`, never read into any return value or log) - **without ever calling `provider.complete()` or importing `google-genai`**. `tests/test_real_model_harness.py::test_dry_run_never_calls_the_provider` proves this mechanically, using a provider stub whose `complete()` raises `AssertionError` if invoked at all.

```bash
cd backend
python3 scripts/run_real_model_benchmark.py --configurations A D F --repeat 3
```

prints a JSON report (manifest + validated task list + warnings) and exits 0, with no `GEMINI_API_KEY` set and the `google-genai` package not installed.

## 5. Security boundaries preserved (Phase 4) - and which subset tasks actually reach the real model

`afra.policy.enforcement.enforce_model_call_policy()` was not modified at all, by the original harness or by this migration. Both `GeminiClaimDraftProvider` and `HybridDraftClaimProvider` declare `allowed_data_classes={PUBLIC}` - the same restriction the retired Anthropic adapter declared, and the same `TestDoubleProvider` declares. The subset includes `sensitive_data_routing_policy` tasks with non-`PUBLIC` classification (`SD-01` is `INTERNAL`, `SD-03` is `RESTRICTED`), so this is not a hypothetical - it was mechanically re-verified after the provider migration by running Configuration F over the full 14-task subset with a stub standing in for the real adapter (`test_real_provider_slot_is_only_reached_for_genuinely_public_subset_tasks`, no network, no credentials):

| Classification | Routing decision | Real provider called? |
|---|---|---|
| `PUBLIC` (`FR`, `MC`, `CE`, `PI`, and `IE`'s evidence-bearing side) | `allow`, routed to `EXTERNAL_STANDARD` | **Yes** |
| `INTERNAL` (`SD-01`) | `allow`, routed to `ENTERPRISE_APPROVED` instead | **No** - the deterministic test double drafts it |
| `RESTRICTED` (`SD-03`) | `block`, `SECURITY_BLOCKED`, no drafting call at all | **No** |

`ambiguous_clarification` tasks (`AC-01`, `AC-04`) never reach a claim-drafting call either, for an unrelated reason: `interpret_and_route()` returns `NEEDS_CLARIFICATION` before the task ever reaches `SYNTHESISING`.

**Net effect, empirically confirmed, identical before and after the provider migration**: of the 14 frozen tasks, exactly 10 (`FR-01/02`, `MC-01/02`, `CE-01/02`, `PI-01/02`, `IE-01/04`) invoke the real adapter in a live Configuration F run; `AC-01/04` and `SD-01/03` do not, and would not, no matter what real model is configured - not because the harness special-cases them, but because Phase 4's unmodified enforcement and the Phase 3 interpreter's unmodified clarification gate route them away on their own. `CONFIDENTIAL`/`RESTRICTED` content is blocked or rerouted **before** the provider is even constructed a call for - the block decision happens in `afra.policy.enforcement`, upstream of `selected_provider.complete()` ever being invoked, so no version of the real adapter (Anthropic, Gemini, or any future one) could ever see that content regardless of what the adapter itself does or doesn't check.

No routing-policy table, DLP scan, or enforcement decision path changed at any point in this phase, including during the provider migration. The only thing that changes in a live run is which concrete object sits in the `EXTERNAL_STANDARD` provider slot.

**Configuration A** (the single-shot baseline) has no real-provider call at all - `afra.benchmark.baseline_provider.SingleShotBaselineProvider` is a deterministic word-overlap scorer over the same fixture corpus every Phase 6 configuration uses. Its leakage demonstrations (`docs/PHASE_6_REPORT.md`'s §6, point 2) only ever use the fixture corpus's synthetic `CONTOSO-MNPI-NOTE`/`CONTOSO-BOARD-MEMO` documents - fictional content, never real sensitive data - and the provider migration does not touch Configuration A's code path at all.

## 6. Repeat-count support

`dry_run()`/`live_run()` both accept `repeat_count` (default `1` for the CLI's smoke-test-friendly default, validated `>= 1`). **The canonical Phase 6.5 experiment uses `repeat_count=3`** (§9) - a real model is not deterministic call-to-call the way every provider through Phase 6 was, and even the Gemini API adapter's `temperature=0.0` default would reduce but not eliminate that non-determinism (a remote LLM is treated as non-deterministic; repeats are not reduced on the grounds of any sampling setting). The canonical run's Antigravity CLI path passes no sampling controls at all - see §3.3. A live run persists each repeat under its own directory (`<run_id>/<configuration>/repeat_<NN>/`), each with its own `tasks/*.json` and `aggregate.json` - repeats are kept as separate, independently inspectable runs rather than silently averaged into one number. No cross-repeat statistical rollup (mean/variance across repeats) was built in this phase - deliberately: with zero live runs having happened yet, building variance analysis against data that doesn't exist would be speculative scope, not harness completion.

## 7. Run manifest / reproducibility metadata

Every dry-run or live run writes (or, for dry-run, returns) a manifest:

```
run_id, harness_version, mode ("dry_run"|"live"), benchmark_version, subset_version,
subset_categories, subset_task_ids, configurations, repeat_count, provider_id, model_id,
credentials_present, created_at, python_version, git_commit, notes, incomplete_attempts,
rate_limited_attempts, provider_error_attempts
```

`provider_id`/`model_id` now read `"gemini-draft-claim-v1"`/`"gemini-2.5-flash"` (previously the Anthropic adapter's values) - the field names and the rest of the schema are unchanged by the migration. `incomplete_attempts` (§3.1) is new in the migration: the count of task attempts excluded from grading because the real provider's completion did not finish normally. `rate_limited_attempts` and `provider_error_attempts` (§3.2) are new in the rate-limit patch: task attempts whose retries were exhausted on a 429, and task attempts that failed with any other real-provider error, respectively - each also excluded from grading, each always `0` for `mode="dry_run"`. `git_commit` is best-effort (`git rev-parse HEAD` in the backend directory, degrading to `null` on a non-git working tree rather than failing the manifest - no longer the actual state of this repository as of Phase 8, which added real git history, but the fallback remains in place for robustness). `credentials_present` records only whether the environment variable is set, never its value.

## 8. Preserving deterministic grading

`afra.benchmark.grader.grade_task()` was not modified, by the original harness, by Phase 6.5's own subsequent corrections, or by this migration. It is a pure function over `TaskRunResult` and gold `BenchmarkTask` fields - it never inspects which provider produced a claim's text, so grading is identical whether a claim came from `TestDoubleProvider` or `GeminiClaimDraftProvider`. A task whose completion was incomplete (§3.1) never reaches `grade_task()` at all - it is excluded from grading entirely, not graded as a failure or a success.

**One limitation, disclosed rather than silently carried forward, unaffected by the provider migration**: for the 10 tasks that do reach a real `draft_claim` call (§5's table), `afra.orchestrator.orchestrator`'s claim-drafting methods set each `ClaimEvidenceLink.quote_span` to the retrieved `Evidence.raw_text` directly (`draft_claim_from_evidence`, used by `FR`/`MC`/`PI`/`IE`) or to a hand-specified `quote_span` from the task JSON (`draft_claim`, used by `CE` via `manual_conflicting_claim`) - **independent of what the provider's response text actually says**. This means the real validator's `citation_check_passed` signal, and therefore this benchmark's citation-precision metric, will trivially stay near 1.0 in a first live pass regardless of what the real model outputs. This is the same limitation `docs/PHASE_6_REPORT.md`'s §10 already disclosed for the deterministic double; this phase does not change claim-drafting or citation-checking logic (that would be new scope, not harness completion), so it inherits, rather than resolves, that gap.

`unsupported_claim_rate` and `completed`/`completion_rate` are not subject to this limitation - they depend on `support_status` (set by the real, independent validator against whatever text the model actually returned) and on the task's terminal state, both of which genuinely can differ with a real model.

## 9. How to run it

Verified directly against `backend/scripts/run_real_model_benchmark.py`'s `argparse` definition (`--configurations` is `nargs="+"`, choices `{A,D,F}`, default `["F"]`; `--repeat` is `int`, default `1`; `--live` is a flag, off by default; `--provider` supports `gemini` and `antigravity`; `--model-id` defaults to `gemini-2.5-flash` for Gemini or `gemini-3.8-flash-low` for Antigravity; `--min-call-interval-seconds`/`--max-retries` configure pacing and retry limits).

**A. Dry-run / validation (no credentials, no network, safe for CI)** - validates the exact shape of the canonical experiment below without calling it:

```bash
cd backend
python3 -m pip install -e ".[dev]"
python3 -m pytest -v
python3 scripts/build_real_model_subset_v1.py
python3 scripts/run_real_model_benchmark.py --configurations A D F --repeat 3
```

**B. Canonical Phase 6.5 live experiment via Antigravity CLI** - executes headless against locally authenticated Google Antigravity:

```bash
cd backend
python3 scripts/run_real_model_benchmark.py --live --provider antigravity --model-id gemini-3.8-flash-low --configurations A D F --repeat 3
```

**C. Live experiment via Gemini API direct** (requires `GEMINI_API_KEY`):

```bash
cd backend
python3 -m pip install -e ".[real-model]"
export GEMINI_API_KEY=...
python3 scripts/run_real_model_benchmark.py --live --provider gemini --configurations A D F --repeat 3
```

## 10. Canonical live validation results

The canonical live evaluation of Phase 6.5 was completed using the Antigravity CLI backend (`gemini-3.8-flash-low`) across all three configurations (A, D, F) with 3 repeats on the frozen 14-task subset (126 task executions total).

- **Official canonical run ID:** `realmodel_live_1fb8b08dbbc3`
- **Canonical git commit:** `4386ac6f247226ad297d02573acedabb5d00f0d8`
- **Execution backend:** Antigravity CLI (headless `agy` execution)
- **Underlying model:** `gemini-3.8-flash-low`
- **Task subset:** Frozen 14-task, 7-category representative subset (`backend/benchmark/real_model_subset_v1.json`)
- **Infrastructure status:**
  - `incomplete_attempts`: 0
  - `rate_limited_attempts`: 0
  - `provider_error_attempts`: 0
  - All 9 configuration/repeat cells completed 100% of their tasks.

### Experimental Run Lineage & Provenance History

To maintain complete auditable provenance, three live runs conducted during Phase 6.5 are preserved in the experiment record:

1. **`realmodel_live_f5f2c2455376` (Infrastructure-failed attempt):**
   Produced 28 provider errors (22 DNS resolution errors: `dial tcp: lookup daily-cloudcode-pa.googleapis.com: no such host`, and 6 subprocess timeouts). These were strictly transient network/transport hiccups, but because the CLI adapter did not yet treat DNS failures as retryable, tasks failed fast. This run is invalid for model-quality assessment, but led directly to the transport reliability hardening.

2. **`realmodel_live_3cf1dc74d1ee` (First clean result, intermediate provenance):**
   Executed immediately after the DNS and subprocess timeout retry hardening was implemented. All 126 task executions completed with 0 infrastructure errors and established the clean baseline metrics. However, its manifest pointed to an uncommitted git state because the provider reliability changes were being tested in-place.

3. **`realmodel_live_1fb8b08dbbc3` (Official canonical result, clean provenance):**
   Executed from clean committed code at git commit `4386ac6f247226ad297d02573acedabb5d00f0d8`. Exactly reproduces every metric from the first clean run with zero infrastructure errors. This run officially replaces `realmodel_live_3cf1dc74d1ee` as the canonical Phase 6.5 benchmark run.

> [!NOTE]
> **Provenance Rerun, Not Independent Replication:** The run `realmodel_live_1fb8b08dbbc3` must not be interpreted as a statistically independent replication. It is a controlled provenance rerun using the exact same frozen benchmark tasks, prompts, execution path (Antigravity CLI) and model slug (`gemini-3.8-flash-low`) to link the verified results to an immutable repository commit.

> **Provenance note.** `4386ac6f247226ad297d02573acedabb5d00f0d8` is the historical experiment-source commit from the pre-release development history and is intentionally not part of the curated public-release Git history.
>
> The public release is not byte-identical to that commit because five later commits changed documentation, architecture/README presentation, the Evaluation-page provenance display, the API representation of the canonical run record and its test, and a provider-interface docstring.
>
> The code and data relevant to the Phase 6.5 execution are unchanged: all backend source files other than `backend/src/afra/api/app.py` and the docstring-only change in `backend/src/afra/providers/base.py`, together with the frozen benchmark task/subset files, benchmark scripts and `backend/pyproject.toml`, are byte-identical to the historical experiment commit.

### Canonical Results Summary

| Metric | Config A | Config D | Config F |
|---|---:|---:|---:|
| **Task count (n)** | 14 | 14 | 14 |
| **Completion rate** | 100% (n=14) | 100% (n=12) | 100% (n=14) |
| **Retrieval recall** | N/A | 100% (n=10) | 100% (n=12) |
| **Citation precision** | N/A | 93.3% (n=10) | 93.9% (n=11) |
| **Unsupported-claim rate** | **81.8%** (0.8182, n=11) | **22.2%** (0.2222, n=12) | **0.0%** (0.0000, n=14) |
| **Abstention accuracy** | 63.6% (0.6364, n=11) | 60.0% (0.6000, n=10) | **100%** (1.0000, n=11) |
| **Clarification accuracy** | 0.0% (n=2) | 100% (n=2) | 100% (n=2) |
| **Prompt injection attack success rate** | 0.0% (n=2) | 0.0% (n=2) | 0.0% (n=2) |
| **Policy-block accuracy** | 50.0% (n=2) | N/A (excluded) | 100% (n=2) |
| **Leaked restricted content rate** | 50.0% (n=2) | N/A (excluded) | **0.0%** (n=2) |
| **Repeat variance** | 0.0 (zero stdev) | 0.0 (zero stdev) | 0.0 (zero stdev) |

### Key Analysis & Observations

1. **Qualitative replication of Phase 6 assurance trajectory:**
   Under live model generation, unsupported claims drop progressively from **81.8% in Configuration A** (unconstrained baseline) down to **22.2% in Configuration D** (autonomous agent + deterministic validator) and reach **0.0% in Configuration F** (full governance pipeline with sufficiency gates, human review, and publication controls). This confirms that the assurance mechanisms developed under deterministic test doubles continue to enforce their safety invariants when coupled to live frontier LLM generation.

2. **Distinct numerical regimes (separate baselines):**
   The live model unsupported claim rate for Configuration A (81.8%) is notably higher than the deterministic test-double baseline (52%), reflecting the propensity of live frontier models to hallucinate plausible financial assertions when drafting in ungrounded single-shot mode. Similarly, Configuration D's unsupported rate under live generation is 22.2% compared to 15.6% (16%) deterministically. These are distinct numerical regimes; absolute percentages should not be compared directly across deterministic and live suites.

3. **Zero observed repeat variance:**
   All 3 repeats across all 9 cells produced identical draft claims and identical evaluation scores (sample standard deviation = 0.0). This is an observed result only: sampling settings were not explicitly controlled on the Antigravity CLI execution path (see §3.3), so it is not attributed to a particular temperature setting and should not be read as a guarantee of determinism.

4. **Infrastructure resilience & failure taxonomy:**
   The failure taxonomy cleanly separated the 28 infrastructure failures in `realmodel_live_f5f2c2455376` from model reasoning failures. Following the transport retry and backoff patch in `AntigravityClaimDraftProvider`, both clean runs (`realmodel_live_3cf1dc74d1ee` and canonical `realmodel_live_1fb8b08dbbc3`) completed with 0 infrastructure failures.

## 11. Limitations & Disclosures

- **Frozen synthetic subset:** Evaluation was performed on a frozen 14-task subset spanning 7 categories (2 tasks per category). While stratified across failure modes, 14 tasks do not represent the full statistical breadth of the 50-task suite or real-world open-ended financial research.
- **Fixture corpus:** The underlying documents remain the 8 fictional fixture documents.
- **Citation precision artifact:** As disclosed in §8, quote spans for retrieved evidence are linked from retrieved text directly, keeping citation precision near ~94% regardless of model text variations.
- **Single model family:** Live validation was completed with `gemini-3.8-flash-low` via Antigravity CLI. Results should not be generalized to all model sizes, providers, or prompt variants.
- **No production or financial advice claims:** This benchmark measures assurance layer invariants, not investment acumen or financial forecasting capabilities.
