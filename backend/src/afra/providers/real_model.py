"""The one real ModelProvider adapter proposed in
docs/PHASE_6_REPORT.md#13-smallest-proposed-real-model-experiment: scoped to
the purpose="draft_claim" call only, for a Phase 6.5 real-model pass.

Provider choice: Google Gemini (`gemini-2.5-flash`, via the `google-genai`
SDK) - migrated from an earlier Anthropic adapter; see
docs/PHASE_6_5_REAL_MODEL_REPORT.md's provider-selection section for the
full rationale (model choice, sampling defaults, Free Tier limitations),
recorded there before this module was changed. `gemini-2.5-flash` was
picked as the smallest model appropriate for a short, single-turn,
evidence-grounded claim-drafting completion - not a frontier/pro-tier
model this experiment's size doesn't need. The planned experiment is small
relative to typical Free Tier limits, subject to the account's current
quota and Google's current rate-limit policy - this module does not
hardcode or promise a specific quota, since the API does not expose one to
check at runtime.

Gemini 2.5 Flash may spend part of `max_output_tokens` on internal
"thinking" tokens before producing an answer, so a response can come back
truncated (finish_reason != "STOP") without a network error. complete()
treats any non-STOP finish_reason as a failed attempt
(IncompleteCompletionError), never as a normal claim to grade - see that
exception's docstring and docs/PHASE_6_5_REAL_MODEL_REPORT.md.

Thinking disabled (added after a live F x1 smoke test showed 8/18 real
draft_claim calls - 44% - hit MAX_TOKENS at max_output_tokens=512, while
every call that *did* finish only ever needed ~19-48 output tokens for a
complete claim; see docs/PHASE_6_5_REAL_MODEL_REPORT.md's rate-limit/
diagnosis sections): complete() explicitly sends
thinking_config=ThinkingConfig(thinking_budget=0) (DEFAULT_THINKING_BUDGET),
disabling Gemini 2.5 Flash's default dynamic/automatic thinking for this
narrow, evidence-grounded, single-sentence claim-drafting purpose - not
open-ended reasoning or planning, which is exactly the purpose this
adapter is scoped to and nothing else (see UnsupportedPurposeError below).
This is a provider-level default (DEFAULT_THINKING_BUDGET), overridable via
the constructor exactly like temperature/max_output_tokens, not a hardcoded
magic value baked into the request.

Token observability (added alongside thinking_budget=0, for the same
diagnosis): complete() now reads response.usage_metadata - including
thoughts_token_count, when the SDK exposes it - for *every* real response,
success or incomplete, before the finish_reason branch decides which one
it is. A MAX_TOKENS response's token accounting is therefore captured too
(prompt_token_count, candidates_token_count, thoughts_token_count,
total_token_count, finish_reason, output_text_length), carried both on the
provider (last_token_usage/last_call_finish_reason/last_output_text_length)
and on IncompleteCompletionError itself, rather than being silently
discarded the way it was before this call was ever made.

Rate-limit handling (added after a first live smoke test hit the Free
Tier's observed 5 requests/minute/project/model quota and crashed with an
unhandled 429 RESOURCE_EXHAUSTED - see docs/PHASE_6_5_REAL_MODEL_REPORT.md's
rate-limiting section): every real network *attempt* - the first one and
each retry - is paced at least `min_call_interval_seconds` (default 15s)
after the previous real network attempt made by this instance, via a single
shared `_last_call_at` timestamp and `_pace()` call at the top of every
iteration of `_generate_with_retries()`'s loop. Within one complete() call,
a 429 is retried up to `max_retries` times (default 3): when the error
response carries the server's own RetryInfo/Retry-After delay, the actual
sleep before the next attempt is `max(server_delay, min_call_interval_seconds)
+ retry_safety_margin_seconds` (default margin 2s) - a live run's real
Free Tier quota was observed reporting a RetryInfo delay of "0s" on
consecutive 429s, and retrying immediately on that literal value produced
another 429 every time (see docs/PHASE_6_5_REAL_MODEL_REPORT.md's rate-
limit diagnosis of run realmodel_live_8ddec45b00dd) - so a server-reported
delay is now a floor input, never trusted below the pacing interval.
Without a server-provided delay, a bounded exponential backoff is used,
also floored at `min_call_interval_seconds` (no separate margin added -
see _generate_with_retries()). This is one unified mechanism, not two: the
computed effective delay for the *next* attempt is simply the minimum
`_pace()` enforces at the top of the next loop iteration, so it is applied
exactly once, never stacked on top of a second independent pacing wait.
This pacing/retry machinery only ever runs inside complete()'s real
network-call path; it never touches
afra.providers.test_double.TestDoubleProvider or any other deterministic
provider, and a freshly constructed GeminiClaimDraftProvider never sleeps
on its first call (there is nothing yet to pace against) - see
_pace()/_generate_with_retries() below and this phase's tests, none of
which perform a real time.sleep. If every retry is exhausted, complete()
raises RateLimitExhaustedError instead of crashing the caller -
afra.benchmark.real_model_harness._run_repeat() catches it per task, the
same way it already catches IncompleteCompletionError, and persists it as
a "rate_limited" (never graded) task result rather than aborting the whole
live run.

Nothing in this module is imported or constructed by any default code path
(afra.benchmark.configurations, afra.benchmark.runner, or any Phase 1-6
test) - it is only reachable through afra.benchmark.real_model_harness and
backend/scripts/run_real_model_benchmark.py. Constructing either class here
makes no network call and needs no credentials or the `google-genai`
package installed; both are only required inside
GeminiClaimDraftProvider.complete(), and only when it is actually asked to
handle purpose="draft_claim". See docs/PHASE_6_5_REAL_MODEL_REPORT.md's
status section for the current state of live calls through this module -
one smoke-test attempt hit the 429 this file's rate-limit handling now
guards against; no canonical experiment has ever been run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Callable

from afra.domain.enums import Classification, ProviderClass
from afra.providers.base import ModelProvider, ModelResponse
from afra.providers.test_double import TestDoubleProvider

DRAFT_CLAIM_PURPOSE = "draft_claim"
DEFAULT_API_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_MODEL_ID = "gemini-2.5-flash"
DEFAULT_ANTIGRAVITY_MODEL_ID = "gemini-3.8-flash-low"
DEFAULT_CLI_TIMEOUT_SECONDS = 60.0
SUPPORTED_REAL_PROVIDERS = ("gemini", "antigravity")
# Low temperature: claim drafting must state one claim strictly from the
# given evidence, not generate creatively - see this module's docstring
# and docs/PHASE_6_5_REAL_MODEL_REPORT.md's model-selection section.
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_OUTPUT_TOKENS = 512
# Disables Gemini 2.5 Flash's default dynamic/automatic thinking - see this
# module's docstring's "Thinking disabled" section for why (narrow,
# evidence-grounded factual claim drafting, not open-ended reasoning).
DEFAULT_THINKING_BUDGET = 0

# Rate-limit pacing/retry defaults - see this module's docstring. The
# observed Free Tier quota is 5 requests/minute/project/model (one call
# roughly every 12s sustainable); 15s is a deliberately safe margin above
# that, not the tightest interval that would technically fit.
DEFAULT_MIN_CALL_INTERVAL_SECONDS = 15.0
DEFAULT_MAX_RETRIES = 3
# Added on top of max(server_delay, min_call_interval_seconds) when a 429
# carries a server-provided RetryInfo/Retry-After delay - a live run
# observed that delay reported as "0s" on consecutive 429s, and retrying
# exactly on the server's literal value (even after flooring it) produced
# another 429 immediately; this margin is deliberate slack, not a rounding
# fudge - see this module's docstring's "Rate-limit handling" section.
DEFAULT_RETRY_SAFETY_MARGIN_SECONDS = 2.0
# Bounded exponential backoff, used only when a 429 response carries no
# server-provided RetryInfo delay to honour instead: 10s, 20s, 40s (capped
# at 60s) - then floored at min_call_interval_seconds the same way a
# server-provided delay is (see _generate_with_retries()).
RETRY_BASE_DELAY_SECONDS = 10.0
RETRY_MAX_DELAY_SECONDS = 60.0


class RealModelCredentialsMissingError(RuntimeError):
    """Raised before any import of the `google-genai` package or network
    attempt - a live call fails fast on a missing credential, not partway
    through a request."""


class AntigravityCliError(RuntimeError):
    """Raised when the Antigravity CLI fails (process error, timeout, malformed output)."""


class UnsupportedPurposeError(RuntimeError):
    """Raised if anything asks this adapter to handle a purpose other than
    "draft_claim" - see this module's docstring and
    docs/PHASE_6_REPORT.md#13 for why the real-model boundary is drawn
    there and nowhere else."""


class IncompleteCompletionError(RuntimeError):
    """Raised when Gemini reports a finish_reason other than "STOP" (most
    notably "MAX_TOKENS" - Gemini 2.5 Flash can spend part of its output
    budget on internal "thinking" tokens before an answer, so this is a
    real, expected failure mode, not a hypothetical). The partial/truncated
    text is never returned as a ModelResponse and must never be graded as a
    normal, successful claim - afra.benchmark.grader (Phase 6, unmodified)
    never sees it at all; afra.benchmark.real_model_harness's live_run()
    catches this per-task, records it in that task's persisted result and
    in the run manifest's `incomplete_attempts` count, and moves on to the
    next task rather than crashing the whole run.
    """

    def __init__(
        self,
        finish_reason: str,
        partial_text: str,
        token_usage: dict | None = None,
        output_text_length: int | None = None,
    ) -> None:
        self.finish_reason = finish_reason
        self.partial_text = partial_text
        # Usage accounting for this exact failed call (prompt_token_count,
        # candidates_token_count, thoughts_token_count, total_token_count) -
        # optional/None only for callers that construct this exception
        # directly without a real Gemini response (test doubles standing in
        # for the real adapter); GeminiClaimDraftProvider.complete() always
        # supplies it. See this module's docstring's "Token observability"
        # section - this is what used to be silently discarded.
        self.token_usage = token_usage
        self.output_text_length = output_text_length if output_text_length is not None else len(partial_text)
        super().__init__(
            f"Gemini completion did not finish normally (finish_reason={finish_reason!r}) - "
            "treated as an incomplete/failed attempt, never graded as a successful claim."
        )


class RateLimitExhaustedError(RuntimeError):
    """Raised when a Gemini call keeps hitting 429 RESOURCE_EXHAUSTED
    through every retry allowed by GeminiClaimDraftProvider.max_retries.
    Like IncompleteCompletionError, this is a real, expected failure mode
    for a Free Tier project (observed quota: 5 requests/minute/project/
    model) - not a hypothetical - and must never be treated as, or graded
    as, a successful claim.
    afra.benchmark.real_model_harness._run_repeat() catches this per task,
    records it in that task's persisted result (status="rate_limited", no
    "result"/"score" key) and in the run manifest's `rate_limited_attempts`
    count, and moves on to the next task rather than crashing the whole
    live run - exactly the same pattern already used for
    IncompleteCompletionError.
    """

    def __init__(self, *, retry_count: int, rate_limit_events: list[dict], last_status: str) -> None:
        self.retry_count = retry_count
        self.rate_limit_events = rate_limit_events
        self.last_status = last_status
        plural = "y" if retry_count == 1 else "ies"
        super().__init__(
            f"Gemini rate limit retries exhausted after {retry_count} retr{plural} "
            f"(last status={last_status!r}) - treated as a failed attempt, never graded "
            "as a successful claim."
        )


RETRYABLE_SERVER_ERROR_CODES = frozenset({500, 502, 503, 504})
RETRYABLE_SERVER_STATUS_NAMES = frozenset({
    "UNAVAILABLE",
    "INTERNAL",
    "DEADLINE_EXCEEDED",
    "BAD_GATEWAY",
    "GATEWAY_TIMEOUT",
})
NON_RETRYABLE_CLIENT_STATUS_NAMES = frozenset({
    "INVALID_ARGUMENT",
    "FAILED_PRECONDITION",
    "OUT_OF_RANGE",
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "NOT_FOUND",
    "ALREADY_EXISTS",
})


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("code", "status_code", "http_status"):
        val = getattr(exc, attr, None)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    """Duck-typed on purpose: checks the `code`/`status` attributes the
    real google-genai SDK's ClientError exposes (code=429,
    status="RESOURCE_EXHAUSTED"), without importing google.genai.errors or
    requiring isinstance against it - so this works identically against a
    fake exception injected by a test with no `google-genai` package
    installed at all, and stays correct even if the SDK's exception class
    hierarchy changes."""
    code = _extract_status_code(exc)
    if code == 429:
        return True
    return getattr(exc, "status", None) == "RESOURCE_EXHAUSTED"


def _is_transient_server_error(exc: Exception) -> bool:
    """Checks whether exc represents a transient server-side failure
    (HTTP 500, 502, 503, 504 or gRPC UNAVAILABLE, INTERNAL, DEADLINE_EXCEEDED,
    BAD_GATEWAY, or google.genai.errors.ServerError), using duck-typing without
    requiring an import of google.genai.errors.
    Never matches client errors (4xx other than 429) or local validation errors."""
    code = _extract_status_code(exc)
    if code is not None:
        if 400 <= code < 500:
            # Deterministic client error - never retry
            return False
        if code in RETRYABLE_SERVER_ERROR_CODES or (500 <= code < 600):
            return True

    status = getattr(exc, "status", None)
    if isinstance(status, str):
        if status in NON_RETRYABLE_CLIENT_STATUS_NAMES:
            return False
        if status in RETRYABLE_SERVER_STATUS_NAMES:
            return True

    name = type(exc).__name__
    if name == "ServerError" or name.endswith("ServerError"):
        return True

    return False


def _is_retryable_provider_error(exc: Exception) -> bool:
    """Returns True for retryable rate-limit (429) and transient server (5xx)
    errors. Returns False for deterministic client failures (4xx other than 429)
    and local validation/code errors."""
    return _is_rate_limit_error(exc) or _is_transient_server_error(exc)


def _parse_seconds_string(value: object) -> float | None:
    """Parses a protobuf Duration-style string (e.g. "20s", "1.5s") - the
    shape google.rpc.RetryInfo.retryDelay is serialized as. Returns None
    (never guesses) for anything else."""
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        return float(value[:-1])
    except ValueError:
        return None


def _parse_retry_delay_seconds(exc: Exception) -> float | None:
    """Best-effort extraction of a server-provided retry delay from a
    Gemini 429 error. Checks the structured google.rpc.RetryInfo shape
    (nested either at the top level of the error's `details`, or under an
    "error" key - both are real shapes the API's JSON error body can take)
    and falls back to an HTTP `Retry-After` header if the exception carries
    the raw response. Returns None if nothing usable is found, so the
    caller falls back to bounded exponential backoff instead - this
    function never invents a delay it has no evidence for.
    """
    details = getattr(exc, "details", None)
    candidates: list[dict] = []
    if isinstance(details, dict):
        top_level = details.get("details")
        if isinstance(top_level, list):
            candidates.extend(item for item in top_level if isinstance(item, dict))
        nested_error = details.get("error")
        if isinstance(nested_error, dict):
            nested_details = nested_error.get("details")
            if isinstance(nested_details, list):
                candidates.extend(item for item in nested_details if isinstance(item, dict))
    for item in candidates:
        if "RetryInfo" in item.get("@type", ""):
            delay = _parse_seconds_string(item.get("retryDelay"))
            if delay is not None:
                return delay

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None and hasattr(headers, "get"):
        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                return None
    return None


class GeminiClaimDraftProvider(ModelProvider):
    """A real Google Gemini API adapter, scoped to purpose="draft_claim"
    only.

    Declares the same provider_class/allowed_data_classes/
    external_network_exposure as afra.providers.test_double.TestDoubleProvider
    (EXTERNAL_STANDARD / PUBLIC-only / network-exposed) - afra.policy.enforcement
    applies to this adapter exactly as it does to the deterministic double it
    stands in for, unchanged. PUBLIC-only is the correct declaration for the
    frozen Phase 6.5 subset (see backend/benchmark/real_model_subset_v1.json)
    - no broadening of what a real, network-exposed provider is trusted with
    is proposed here.
    """

    provider_id = "gemini-draft-claim-v1"
    provider_class = ProviderClass.EXTERNAL_STANDARD
    allowed_data_classes = frozenset({Classification.PUBLIC})
    external_network_exposure = True

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        temperature: float = DEFAULT_TEMPERATURE,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        thinking_budget: int = DEFAULT_THINKING_BUDGET,
        min_call_interval_seconds: float = DEFAULT_MIN_CALL_INTERVAL_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_safety_margin_seconds: float = DEFAULT_RETRY_SAFETY_MARGIN_SECONDS,
        sleep_fn: Callable[[float], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        self.model_id = model_id
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        # 0 disables Gemini's dynamic/automatic thinking - see this
        # module's docstring's "Thinking disabled" section.
        self.thinking_budget = thinking_budget
        # Pacing/retry configuration - see this module's docstring.
        # sleep_fn/clock_fn default to real time.sleep/time.monotonic and
        # exist only so tests can inject fakes; production code never needs
        # to pass them.
        self.min_call_interval_seconds = min_call_interval_seconds
        self.max_retries = max_retries
        self.retry_safety_margin_seconds = retry_safety_margin_seconds
        self._sleep: Callable[[float], None] = sleep_fn or time.sleep
        self._clock: Callable[[], float] = clock_fn or time.monotonic
        # Set only after a real network attempt (success or failure) -
        # None means "no previous call to pace against", so a freshly
        # constructed provider's first call never sleeps.
        self._last_call_at: float | None = None
        self.call_count = 0
        self.calls: list[tuple[str, str]] = []
        # The finish_reason of the most recent SUCCESSFUL (STOP) call only,
        # or None before any call has completed normally - legacy field,
        # kept as-is for backward compatibility. Use last_call_finish_reason
        # below for the outcome of the most recent call regardless of
        # success/failure.
        self.last_finish_reason: str | None = None
        # Observability fields for the most recent complete() call,
        # regardless of whether it succeeded, came back incomplete, or
        # exhausted its rate-limit retries - read by
        # afra.benchmark.real_model_harness the same way as
        # last_finish_reason, always via getattr(..., None) so a provider
        # that doesn't set them (any deterministic test double) never
        # breaks the harness.
        self.last_retry_count: int = 0
        self.last_rate_limit_events: list[dict] = []
        self.last_provider_status: str = "unknown"
        self.last_latency_ms: int | None = None
        # The finish_reason of the most recent call, success or not (unlike
        # last_finish_reason above) - e.g. "MAX_TOKENS" after an incomplete
        # call, not just None. See this module's docstring's "Token
        # observability" section.
        self.last_call_finish_reason: str | None = None
        # {"prompt_token_count", "candidates_token_count",
        # "thoughts_token_count", "total_token_count"} from the most recent
        # real response's usage_metadata - captured for both a successful
        # and an incomplete (e.g. MAX_TOKENS) response, read before the
        # finish_reason branch in complete() decides which one it is. None
        # only when no real response was ever obtained (e.g. rate-limit
        # retries exhausted) or the SDK didn't expose usage_metadata at all.
        self.last_token_usage: dict | None = None
        # len(response.text or "") for the most recent real response,
        # success or incomplete - None only when no real response was ever
        # obtained.
        self.last_output_text_length: int | None = None

    def credentials_present(self) -> bool:
        """Only checks whether the env var is set - never reads it into a
        return value, logs it, or otherwise exposes it."""
        return bool(os.environ.get(self.api_key_env))

    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        if purpose != DRAFT_CLAIM_PURPOSE:
            raise UnsupportedPurposeError(
                f"{type(self).__name__} only handles purpose={DRAFT_CLAIM_PURPOSE!r}, "
                f"got {purpose!r} - see afra.providers.real_model's module docstring."
            )
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RealModelCredentialsMissingError(
                f"${self.api_key_env} is not set - refusing to attempt a live call. "
                "See docs/PHASE_6_5_REAL_MODEL_REPORT.md for the live-run command."
            )
        # Imported here, not at module load time, so importing this module
        # (every dry-run path does) never requires the `google-genai`
        # package - see pyproject.toml's `real-model` extra.
        from google import genai  # type: ignore[import-not-found]
        from google.genai import types as genai_types  # type: ignore[import-not-found]

        client = genai.Client(api_key=api_key)
        self.call_count += 1
        self.calls.append((purpose, prompt))
        # Pacing (_pace()) is enforced inside _generate_with_retries(), at
        # the top of *every* loop iteration - the first attempt and every
        # retry alike, all through the same shared _last_call_at clock. Not
        # called here directly: a single call site covers every real HTTP
        # attempt this method could possibly make, instead of splitting
        # "first attempt paced here, retries paced differently there" -
        # see this module's docstring's "Rate-limit handling" section.
        # latency_ms below therefore includes any pacing/retry wait this
        # call needed, not just raw HTTP time - an honest total cost for
        # this draft_claim call, not a number that quietly excludes what
        # rate-limiting made it wait for.
        overall_started = self._clock()
        try:
            response, retry_count, rate_limit_events = self._generate_with_retries(client, genai_types, prompt)
        except RateLimitExhaustedError as exc:
            self.last_retry_count = exc.retry_count
            self.last_rate_limit_events = exc.rate_limit_events
            self.last_provider_status = "rate_limited"
            self.last_latency_ms = int((self._clock() - overall_started) * 1000)
            # No real response was ever obtained - nothing to read.
            self.last_call_finish_reason = None
            self.last_token_usage = None
            self.last_output_text_length = None
            raise
        except Exception as exc:
            self.last_retry_count = getattr(self, "last_retry_count", 0)
            self.last_rate_limit_events = getattr(self, "last_rate_limit_events", [])
            self.last_provider_status = "provider_error"
            self.last_latency_ms = int((self._clock() - overall_started) * 1000)
            self.last_call_finish_reason = None
            self.last_token_usage = None
            self.last_output_text_length = None
            raise
        latency_ms = int((self._clock() - overall_started) * 1000)

        # finish_reason lives on the first candidate, not on the response
        # top-level - normalized to a plain string (enum members expose
        # `.name`; some SDK/mock shapes may just be a string already).
        # None (no candidates, or the field genuinely absent) is passed
        # through rather than treated as failure - the API doesn't
        # guarantee this is always populated, and this adapter should not
        # invent a failure it has no evidence for.
        finish_reason: str | None = None
        candidates = getattr(response, "candidates", None)
        if candidates:
            raw_reason = getattr(candidates[0], "finish_reason", None)
            if raw_reason is not None:
                finish_reason = getattr(raw_reason, "name", None) or str(raw_reason)

        # Token observability: read usage_metadata for *every* real
        # response, before deciding success vs. incomplete below - this is
        # exactly the read that used to be skipped entirely on a MAX_TOKENS
        # response (see this module's docstring's "Token observability"
        # section). getattr(..., None) throughout: not every SDK/mock shape
        # populates every field (thoughts_token_count in particular is only
        # present "if applicable"), and a missing field is not evidence of
        # a failure.
        output_text = response.text or ""
        usage = getattr(response, "usage_metadata", None)
        token_usage = {
            "prompt_token_count": getattr(usage, "prompt_token_count", None),
            "candidates_token_count": getattr(usage, "candidates_token_count", None),
            "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
            "total_token_count": getattr(usage, "total_token_count", None),
        }
        output_text_length = len(output_text)

        self.last_retry_count = retry_count
        self.last_rate_limit_events = rate_limit_events
        self.last_latency_ms = latency_ms
        self.last_call_finish_reason = finish_reason
        self.last_token_usage = token_usage
        self.last_output_text_length = output_text_length

        if finish_reason is not None and finish_reason != "STOP":
            self.last_provider_status = "incomplete"
            raise IncompleteCompletionError(
                finish_reason=finish_reason,
                partial_text=output_text,
                token_usage=token_usage,
                output_text_length=output_text_length,
            )

        self.last_finish_reason = finish_reason
        self.last_provider_status = "ok"
        tokens_in = token_usage["prompt_token_count"] or 0
        tokens_out = token_usage["candidates_token_count"] or 0
        return ModelResponse(
            text=output_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            # No priced-cost model is wired up yet - see
            # docs/PHASE_6_5_REAL_MODEL_REPORT.md's cost caveat, carried
            # forward from docs/PHASE_6_REPORT.md's §10.
            cost=0.0,
        )

    def _pace(self, minimum_seconds: float) -> None:
        """Blocks until at least `minimum_seconds` has elapsed since the
        last real network attempt made by this instance - or returns
        immediately if there was none yet, or `minimum_seconds <= 0` (used
        by tests to disable pacing entirely). Called at the top of *every*
        iteration of _generate_with_retries()'s loop - the first attempt
        and every retry alike - through the single shared `_last_call_at`
        clock, so no real HTTP attempt this instance ever makes (for any
        task, in any complete() call) is spaced less than `minimum_seconds`
        after the previous one. `minimum_seconds` is normally
        `self.min_call_interval_seconds` for a first attempt, or the
        already-floored/margined effective retry delay for a retry - see
        _generate_with_retries() - never stacked on top of a second,
        separate pacing wait for the same attempt.
        """
        if minimum_seconds <= 0 or self._last_call_at is None:
            return
        remaining = minimum_seconds - (self._clock() - self._last_call_at)
        if remaining > 0:
            self._sleep(remaining)

    def _generate_with_retries(
        self, client, genai_types, prompt: str
    ) -> tuple[object, int, list[dict]]:
        """Calls client.models.generate_content(), retrying on 429
        RESOURCE_EXHAUSTED or transient 5xx server errors (500, 502, 503, 504,
        UNAVAILABLE, INTERNAL, etc.) up to self.max_retries times.
        Deterministic client errors (4xx other than 429, auth failures,
        malformed requests) and unexpected local exceptions propagate
        immediately, unretried - afra.benchmark.real_model_harness catches
        those at the task level instead (status="provider_error"), rather than
        this adapter guessing which other failures are safe to retry.

        Pacing: _pace() is called at the top of *every* iteration of this
        loop, with a floor that starts at `min_call_interval_seconds` (for
        the first attempt) and becomes the previous attempt's own computed
        effective delay after a retryable error (for the next retry) - see
        `next_attempt_minimum` below. A live run observed the server's own
        RetryInfo delay reported as "0s" on consecutive 429s within an
        already-saturated quota window; trusting that literally meant
        retrying essentially instantly, which produced another 429 every
        time (see docs/PHASE_6_5_REAL_MODEL_REPORT.md's diagnosis of run
        realmodel_live_8ddec45b00dd). So a server-provided delay is now a
        floor input, not the sleep duration itself:
        `effective_delay = max(server_delay, min_call_interval_seconds)
        + retry_safety_margin_seconds`. Without a server-provided delay, the
        bounded exponential backoff is floored the same way (no separate
        margin - only a server-reported "you can retry now" value has been
        observed to be unreliable, not the backoff schedule this adapter
        itself computes).
        """
        retry_count = 0
        rate_limit_events: list[dict] = []
        self.last_retry_count = 0
        self.last_rate_limit_events = rate_limit_events
        self.last_provider_status = "unknown"
        self.last_call_finish_reason = None
        self.last_token_usage = None
        self.last_output_text_length = None

        # The floor _pace() enforces before the *next* attempt - starts at
        # the ordinary inter-call pacing interval for this call's first
        # attempt, then becomes each retry's own effective delay in turn.
        next_attempt_minimum = self.min_call_interval_seconds
        while True:
            self._pace(next_attempt_minimum)
            try:
                response = client.models.generate_content(
                    model=self.model_id,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        temperature=self.temperature,
                        max_output_tokens=self.max_output_tokens,
                        # Disables dynamic/automatic thinking - see this
                        # module's docstring's "Thinking disabled" section.
                        thinking_config=genai_types.ThinkingConfig(thinking_budget=self.thinking_budget),
                    ),
                )
                self._last_call_at = self._clock()
                self.last_retry_count = retry_count
                self.last_rate_limit_events = rate_limit_events
                return response, retry_count, rate_limit_events
            except Exception as exc:
                self._last_call_at = self._clock()
                if not _is_retryable_provider_error(exc):
                    self.last_retry_count = retry_count
                    self.last_rate_limit_events = rate_limit_events
                    raise

                code = _extract_status_code(exc)
                status = getattr(exc, "status", None)
                if status is None:
                    if _is_rate_limit_error(exc):
                        status = "RESOURCE_EXHAUSTED"
                    elif code is not None:
                        status = f"HTTP_{code}"
                    else:
                        status = type(exc).__name__

                if retry_count >= self.max_retries:
                    self.last_retry_count = retry_count
                    self.last_rate_limit_events = rate_limit_events
                    if _is_rate_limit_error(exc):
                        raise RateLimitExhaustedError(
                            retry_count=retry_count, rate_limit_events=rate_limit_events, last_status=status
                        ) from exc
                    # Transient server error (5xx) with retries exhausted:
                    # re-raise the underlying exception so the harness records
                    # it as a provider_error attempt, never graded as model quality.
                    raise exc

                server_delay = _parse_retry_delay_seconds(exc)
                if server_delay is not None:
                    effective_delay = max(server_delay, self.min_call_interval_seconds) + self.retry_safety_margin_seconds
                    source = "server"
                else:
                    backoff = min(RETRY_BASE_DELAY_SECONDS * (2**retry_count), RETRY_MAX_DELAY_SECONDS)
                    effective_delay = max(backoff, self.min_call_interval_seconds)
                    source = "backoff"

                rate_limit_events.append(
                    {
                        "attempt": retry_count + 1,
                        # The effective (floored + margined) delay actually
                        # enforced before the next attempt - not the raw,
                        # possibly-0s server value.
                        "delay_seconds": effective_delay,
                        "server_delay_seconds": server_delay,
                        "source": source,
                        "status": status,
                    }
                )
                # No explicit sleep here: the next loop iteration's _pace()
                # call sleeps the (remaining portion of) effective_delay,
                # since _last_call_at was just updated above - one sleep
                # per attempt, not a separate retry-delay sleep stacked on
                # top of a second pacing wait.
                next_attempt_minimum = effective_delay
                retry_count += 1
                self.last_retry_count = retry_count
                self.last_rate_limit_events = rate_limit_events


def _parse_cli_json_output(stdout: str) -> dict:
    """Parses JSON from agy CLI stdout, allowing for leading or trailing whitespace."""
    cleaned = stdout.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise


class AntigravityClaimDraftProvider(ModelProvider):
    """A real Google Antigravity CLI adapter, scoped to purpose="draft_claim" only.

    Executes non-interactive print mode on the locally installed `agy` binary:
    `agy -p "<prompt>" --output-format json --model <model-slug> --disable-slash-commands`

    Declares EXTERNAL_STANDARD / PUBLIC-only / network-exposed policy properties
    identically to GeminiClaimDraftProvider - afra.policy.enforcement applies to
    this adapter identically.
    """

    provider_id = "antigravity-cli-draft-claim-v1"
    provider_class = ProviderClass.EXTERNAL_STANDARD
    allowed_data_classes = frozenset({Classification.PUBLIC})
    external_network_exposure = True

    def __init__(
        self,
        model_id: str = DEFAULT_ANTIGRAVITY_MODEL_ID,
        binary_path: str = "agy",
        timeout_seconds: float = DEFAULT_CLI_TIMEOUT_SECONDS,
        min_call_interval_seconds: float = DEFAULT_MIN_CALL_INTERVAL_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep_fn: Callable[[float], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
        subprocess_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        credentials_checker: Callable[[], bool] | None = None,
    ) -> None:
        self.model_id = model_id
        self.binary_path = binary_path
        self.timeout_seconds = timeout_seconds
        self.min_call_interval_seconds = min_call_interval_seconds
        self.max_retries = max_retries
        self._sleep: Callable[[float], None] = sleep_fn or time.sleep
        self._clock: Callable[[], float] = clock_fn or time.monotonic
        self._runner: Callable[..., subprocess.CompletedProcess] = subprocess_runner or subprocess.run
        self._credentials_checker: Callable[[], bool] | None = credentials_checker
        self._last_call_at: float | None = None
        self.call_count = 0
        self.calls: list[tuple[str, str]] = []
        self.last_finish_reason: str | None = None
        self.last_retry_count: int = 0
        self.last_rate_limit_events: list[dict] = []
        self.last_provider_status: str = "unknown"
        self.last_latency_ms: int | None = None
        self.last_call_finish_reason: str | None = None
        self.last_token_usage: dict | None = None
        self.last_output_text_length: int | None = None
        self._credentials_cached: bool | None = None

    def credentials_present(self) -> bool:
        """Determines whether the agy CLI binary is present and authenticated."""
        if self._credentials_checker is not None:
            return self._credentials_checker()
        if self._credentials_cached is not None:
            return self._credentials_cached

        resolved_bin = shutil.which(self.binary_path)
        if not resolved_bin:
            self._credentials_cached = False
            return False
        try:
            res = self._runner(
                [resolved_bin, "-p", "/model", "--output-format", "json"],
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            if res.returncode != 0:
                self._credentials_cached = False
                return False
            data = json.loads(res.stdout.strip())
            is_ok = data.get("status") == "SUCCESS"
            self._credentials_cached = is_ok
            return is_ok
        except Exception:
            self._credentials_cached = False
            return False

    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        if purpose != DRAFT_CLAIM_PURPOSE:
            raise UnsupportedPurposeError(
                f"{type(self).__name__} only handles purpose={DRAFT_CLAIM_PURPOSE!r}, "
                f"got {purpose!r} - see afra.providers.real_model's module docstring."
            )
        if not self.credentials_present():
            raise RealModelCredentialsMissingError(
                f"{type(self).__name__} is unavailable: 'agy' CLI binary is missing or unauthenticated. "
                "Ensure `agy` is installed in PATH and authenticated via interactive `agy` session."
            )

        self.call_count += 1
        self.calls.append((purpose, prompt))
        overall_started = self._clock()
        try:
            data, retry_count, rate_limit_events = self._generate_with_retries(prompt)
        except RateLimitExhaustedError as exc:
            self.last_retry_count = exc.retry_count
            self.last_rate_limit_events = exc.rate_limit_events
            self.last_provider_status = "rate_limited"
            self.last_latency_ms = int((self._clock() - overall_started) * 1000)
            self.last_call_finish_reason = None
            self.last_token_usage = None
            self.last_output_text_length = None
            raise
        except Exception as exc:
            self.last_retry_count = getattr(self, "last_retry_count", 0)
            self.last_rate_limit_events = getattr(self, "last_rate_limit_events", [])
            self.last_provider_status = "provider_error"
            self.last_latency_ms = int((self._clock() - overall_started) * 1000)
            self.last_call_finish_reason = None
            self.last_token_usage = None
            self.last_output_text_length = None
            raise

        latency_ms = int((self._clock() - overall_started) * 1000)
        output_text = data.get("response") or data.get("result") or data.get("text") or ""
        finish_reason = data.get("finish_reason") or "STOP"

        usage = data.get("usage") or {}
        token_usage = {
            "prompt_token_count": usage.get("input_tokens") if "input_tokens" in usage else usage.get("prompt_token_count"),
            "candidates_token_count": usage.get("output_tokens") if "output_tokens" in usage else usage.get("candidates_token_count"),
            "thoughts_token_count": usage.get("thinking_tokens") if "thinking_tokens" in usage else usage.get("thoughts_token_count"),
            "total_token_count": usage.get("total_tokens") if "total_tokens" in usage else usage.get("total_token_count"),
        }
        output_text_length = len(output_text)

        self.last_retry_count = retry_count
        self.last_rate_limit_events = rate_limit_events
        self.last_latency_ms = latency_ms
        self.last_call_finish_reason = finish_reason
        self.last_token_usage = token_usage
        self.last_output_text_length = output_text_length

        if finish_reason != "STOP":
            self.last_provider_status = "incomplete"
            raise IncompleteCompletionError(
                finish_reason=finish_reason,
                partial_text=output_text,
                token_usage=token_usage,
                output_text_length=output_text_length,
            )

        self.last_finish_reason = finish_reason
        self.last_provider_status = "ok"
        tokens_in = token_usage["prompt_token_count"] or 0
        tokens_out = token_usage["candidates_token_count"] or 0
        return ModelResponse(
            text=output_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost=0.0,
        )

    def _pace(self, minimum_seconds: float) -> None:
        if minimum_seconds <= 0 or self._last_call_at is None:
            return
        remaining = minimum_seconds - (self._clock() - self._last_call_at)
        if remaining > 0:
            self._sleep(remaining)

    def _classify_cli_failure(
        self, returncode: int, stdout: str, stderr: str, json_status: str | None = None
    ) -> tuple[str, bool, str]:
        combined = f"{stdout}\n{stderr}\n{json_status or ''}".lower()
        err_msg = (
            stderr.strip()
            or stdout.strip()
            or (f"Status: {json_status}" if json_status else f"Process exited with code {returncode}")
        )

        # 1. Auth / Session errors (deterministic, never retry)
        auth_indicators = (
            "not logged in",
            "unauthenticated",
            "login required",
            "please log in",
            "session expired",
            "auth error",
            "authentication error",
            "authentication failed",
            "authentication required",
            "permission denied",
            "unauthorized",
        )
        if any(ind in combined for ind in auth_indicators):
            return "auth_error", False, err_msg

        # 2. Model errors (deterministic, never retry)
        model_indicators = (
            "model not found",
            "unknown model",
            "invalid model",
            "model does not exist",
            "model is not supported",
            "unsupported model",
        )
        if any(ind in combined for ind in model_indicators):
            return "invalid_model", False, err_msg

        # 3. CLI usage / syntax / malformed request errors (deterministic, never retry)
        usage_indicators = (
            "flags provided but not defined",
            "flag provided but not defined",
            "unknown flag",
            "usage of agy",
            "invalid argument",
            "bad request",
            "malformed request",
            "unrecognized argument",
            "command not found",
        )
        if any(ind in combined for ind in usage_indicators):
            return "usage_error", False, err_msg

        # 4. Rate limit / Quota exhaustion (retryable)
        rate_limit_indicators = (
            "quota exceeded",
            "quota exhausted",
            "rate limit",
            "rate_limit",
            "resource exhausted",
            "resourceexhausted",
            "too many requests",
            "429",
        )
        if any(ind in combined for ind in rate_limit_indicators):
            return "rate_limited", True, err_msg

        # 5. Transient network / DNS / transport / connection failures (retryable)
        network_indicators = (
            # DNS / name resolution failures
            "no such host",
            "temporary failure in name resolution",
            "name resolution",
            "lookup ",
            "dns lookup",
            "dns resolution",
            "could not resolve host",
            "failed to resolve",
            "nodename nor servname provided",
            "getaddrinfo",
            # Connection reset / peer reset
            "connection reset",
            "connection reset by peer",
            "read: connection reset",
            # Connection refused (plausibly transient)
            "connection refused",
            "connect: connection refused",
            # Network unreachable / routing
            "network is unreachable",
            "network unreachable",
            "no route to host",
            "host is down",
            "network down",
            # TLS handshake timeout
            "tls handshake timeout",
            "handshake timeout",
            "tls timeout",
            # Transport-level EOF & temporary connection drops
            "unexpected eof",
            "transport-level eof",
            "transport is closing",
            "transport: close_notify",
            "transport: error while dialing",
            "broken pipe",
            "network pipe closed",
            "pipe closed",
            "connection closed",
            "connection closed by remote host",
            "client connection is closing",
            "failed to connect",
            "temporary connection failure",
            "temporary failure",
            "dial tcp",
            "dial error",
            "socket hang up",
            "socket timeout",
            "connection error",
            "network error",
            "read: connection timed out",
            "connect: connection timed out",
            "i/o timeout",
            "etimedout",
            "econnreset",
            "econnrefused",
            "ehostunreach",
            "enetunreach",
        )
        if any(ind in combined for ind in network_indicators):
            return "network_error", True, err_msg

        # 6. Transient server / overload / temporary backend error (retryable)
        server_error_indicators = (
            "overloaded",
            "high demand",
            "temporarily unavailable",
            "service unavailable",
            "503 unavailable",
            "503 service unavailable",
            "503",
            "502 bad gateway",
            "502",
            "504 gateway timeout",
            "504",
            "backend error",
            "server error",
            "internal server error",
            "deadline exceeded",
            "gateway timeout",
            "rpc error: code = unavailable",
            "rpc error: code = internal",
        )
        if any(ind in combined for ind in server_error_indicators):
            return "server_error", True, err_msg

        return "cli_error", False, err_msg

    def _compute_backoff_delay(self, retry_count: int) -> float:
        backoff = min(RETRY_BASE_DELAY_SECONDS * (2**retry_count), RETRY_MAX_DELAY_SECONDS)
        return max(backoff, self.min_call_interval_seconds)

    def _record_retry_or_raise(
        self, category: str, err_msg: str, retry_count: int, rate_limit_events: list[dict]
    ) -> None:
        if retry_count >= self.max_retries:
            self.last_retry_count = retry_count
            self.last_rate_limit_events = rate_limit_events
            if category == "rate_limited":
                raise RateLimitExhaustedError(
                    retry_count=retry_count,
                    rate_limit_events=rate_limit_events,
                    last_status="RESOURCE_EXHAUSTED",
                )
            raise AntigravityCliError(
                f"Antigravity CLI failed after {retry_count} retries ({category}): {err_msg}"
            )
        effective_delay = self._compute_backoff_delay(retry_count)
        if category == "rate_limited":
            status_label = "RESOURCE_EXHAUSTED"
        elif category == "timeout":
            status_label = "TIMEOUT"
        elif category == "network_error":
            status_label = "NETWORK_ERROR"
        else:
            status_label = "UNAVAILABLE"

        rate_limit_events.append(
            {
                "attempt": retry_count + 1,
                "delay_seconds": effective_delay,
                "server_delay_seconds": None,
                "source": "backoff",
                "status": status_label,
            }
        )

    def _generate_with_retries(self, prompt: str) -> tuple[dict, int, list[dict]]:
        retry_count = 0
        rate_limit_events: list[dict] = []
        self.last_retry_count = 0
        self.last_rate_limit_events = rate_limit_events
        self.last_provider_status = "unknown"
        self.last_call_finish_reason = None
        self.last_token_usage = None
        self.last_output_text_length = None

        next_attempt_minimum = self.min_call_interval_seconds
        while True:
            self._pace(next_attempt_minimum)
            try:
                proc = self._runner(
                    [
                        self.binary_path,
                        "-p",
                        prompt,
                        "--output-format",
                        "json",
                        "--model",
                        self.model_id,
                        "--disable-slash-commands",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
                self._last_call_at = self._clock()

                if proc.returncode != 0:
                    category, retryable, err_msg = self._classify_cli_failure(
                        proc.returncode, proc.stdout, proc.stderr
                    )
                    if not retryable:
                        raise AntigravityCliError(f"Antigravity CLI failed (exit {proc.returncode}): {err_msg}")
                    self._record_retry_or_raise(
                        category=category,
                        err_msg=err_msg,
                        retry_count=retry_count,
                        rate_limit_events=rate_limit_events,
                    )
                    effective_delay = self._compute_backoff_delay(retry_count)
                    next_attempt_minimum = effective_delay
                    retry_count += 1
                    self.last_retry_count = retry_count
                    self.last_rate_limit_events = rate_limit_events
                    continue

                try:
                    data = _parse_cli_json_output(proc.stdout)
                except Exception as json_err:
                    raise AntigravityCliError(
                        f"Antigravity CLI emitted malformed JSON output: {json_err}. Raw stdout: {proc.stdout!r}"
                    ) from json_err

                if data.get("status") not in (None, "SUCCESS"):
                    category, retryable, err_msg = self._classify_cli_failure(
                        0, proc.stdout, proc.stderr, json_status=data.get("status")
                    )
                    if not retryable:
                        raise AntigravityCliError(
                            f"Antigravity CLI reported failure status {data.get('status')!r}: {err_msg}"
                        )
                    self._record_retry_or_raise(
                        category=category,
                        err_msg=err_msg,
                        retry_count=retry_count,
                        rate_limit_events=rate_limit_events,
                    )
                    effective_delay = self._compute_backoff_delay(retry_count)
                    next_attempt_minimum = effective_delay
                    retry_count += 1
                    self.last_retry_count = retry_count
                    self.last_rate_limit_events = rate_limit_events
                    continue

                self.last_retry_count = retry_count
                self.last_rate_limit_events = rate_limit_events
                return data, retry_count, rate_limit_events

            except subprocess.TimeoutExpired as exc:
                self._last_call_at = self._clock()
                category = "timeout"
                err_msg = f"CLI command timed out after {self.timeout_seconds}s"
                self._record_retry_or_raise(
                    category=category,
                    err_msg=err_msg,
                    retry_count=retry_count,
                    rate_limit_events=rate_limit_events,
                )
                effective_delay = self._compute_backoff_delay(retry_count)
                next_attempt_minimum = effective_delay
                retry_count += 1
                self.last_retry_count = retry_count
                self.last_rate_limit_events = rate_limit_events


class HybridDraftClaimProvider(ModelProvider):
    """Routes purpose="draft_claim" to a real adapter and every other
    purpose (plan/interpret) to a deterministic test double - the exact
    scope docs/PHASE_6_REPORT.md#13 proposed ("Implement exactly one real
    ModelProvider adapter ... for the purpose="draft_claim" call only").

    This is what gets substituted into the EXTERNAL_STANDARD slot of
    afra.benchmark.configurations.build_full_provider_registry() for a live
    Phase 6.5 run: planning/interpretation/routing stay fully deterministic
    and reproducible, only claim-drafting text is real. Grading itself stays
    deterministic regardless (afra.benchmark.grader never grades model
    text directly - see docs/PHASE_6_5_REAL_MODEL_REPORT.md's citation-
    precision caveat). Provider-agnostic - unchanged by the Anthropic ->
    Gemini migration; it only ever depends on the ModelProvider interface.
    """

    provider_class = ProviderClass.EXTERNAL_STANDARD
    allowed_data_classes = frozenset({Classification.PUBLIC})
    external_network_exposure = True

    def __init__(self, real_provider: ModelProvider, fallback: ModelProvider | None = None) -> None:
        self.real_provider = real_provider
        self.fallback = fallback or TestDoubleProvider()
        self.provider_id = f"hybrid[{self.real_provider.provider_id}+{self.fallback.provider_id}]"
        self.model_id = self.real_provider.model_id

    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        if purpose == DRAFT_CLAIM_PURPOSE:
            return self.real_provider.complete(purpose, prompt)
        return self.fallback.complete(purpose, prompt)


def build_real_provider(
    provider_name: str = "gemini",
    *,
    model_id: str | None = None,
    min_call_interval_seconds: float = DEFAULT_MIN_CALL_INTERVAL_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_safety_margin_seconds: float = DEFAULT_RETRY_SAFETY_MARGIN_SECONDS,
    sleep_fn: Callable[[float], None] | None = None,
    clock_fn: Callable[[], float] | None = None,
    binary_path: str = "agy",
    timeout_seconds: float = DEFAULT_CLI_TIMEOUT_SECONDS,
    subprocess_runner: Callable[..., subprocess.CompletedProcess] | None = None,
    credentials_checker: Callable[[], bool] | None = None,
) -> GeminiClaimDraftProvider | AntigravityClaimDraftProvider:
    """Builds a real ModelProvider adapter for Phase 6.5 benchmarking.

    Supports 'gemini' (default, API) and 'antigravity' (local headless CLI).
    """
    name = provider_name.lower().strip()
    if name == "gemini":
        return GeminiClaimDraftProvider(
            model_id=model_id or DEFAULT_MODEL_ID,
            min_call_interval_seconds=min_call_interval_seconds,
            max_retries=max_retries,
            retry_safety_margin_seconds=retry_safety_margin_seconds,
            sleep_fn=sleep_fn,
            clock_fn=clock_fn,
        )
    elif name == "antigravity":
        return AntigravityClaimDraftProvider(
            model_id=model_id or DEFAULT_ANTIGRAVITY_MODEL_ID,
            binary_path=binary_path,
            timeout_seconds=timeout_seconds,
            min_call_interval_seconds=min_call_interval_seconds,
            max_retries=max_retries,
            sleep_fn=sleep_fn,
            clock_fn=clock_fn,
            subprocess_runner=subprocess_runner,
            credentials_checker=credentials_checker,
        )
    else:
        raise ValueError(
            f"Unknown real-model provider: {provider_name!r}. Supported: {', '.join(SUPPORTED_REAL_PROVIDERS)}."
        )
