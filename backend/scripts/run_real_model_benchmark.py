"""Phase 6.5 CLI entry point for the real-model harness - see
docs/PHASE_6_5_REAL_MODEL_REPORT.md.

Default (no flags) is dry-run: validates the frozen subset, configurations,
and repeat-count, and reports whether credentials are present, without
making any external call.

    python3 scripts/run_real_model_benchmark.py --configurations A D F --repeat 3

Providers supported:
    --provider gemini (default, uses Gemini API via GEMINI_API_KEY)
    --provider antigravity (uses local authenticated agy CLI)

Model selection via `--model-id`:
    Gemini default: gemini-2.5-flash
    Antigravity default: gemini-3.8-flash-low (or any model from `agy models`, e.g. claude-sonnet-4-6)

Examples:

    # Gemini live smoke test:
    GEMINI_API_KEY=... python3 scripts/run_real_model_benchmark.py --live \
        --provider gemini --configurations F --repeat 1

    # Antigravity CLI live smoke test:
    python3 scripts/run_real_model_benchmark.py --live \
        --provider antigravity --model-id gemini-3.8-flash-low --configurations F --repeat 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from afra.benchmark.real_model_harness import (
    SUPPORTED_CONFIGURATIONS,
    dry_run,
    live_run,
    load_subset,
)
from afra.benchmark.schema import load_benchmark
from afra.providers.real_model import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_CALL_INTERVAL_SECONDS,
    DEFAULT_RETRY_SAFETY_MARGIN_SECONDS,
    SUPPORTED_REAL_PROVIDERS,
    build_real_provider,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--provider", choices=list(SUPPORTED_REAL_PROVIDERS), default="gemini",
        help=f"Which real-model provider adapter to use (default 'gemini', choices: {', '.join(SUPPORTED_REAL_PROVIDERS)}).",
    )
    parser.add_argument(
        "--live", action="store_true", help="Actually call the real model. Off by default (dry-run)."
    )
    parser.add_argument(
        "--configurations", nargs="+", default=["F"], choices=list(SUPPORTED_CONFIGURATIONS),
        help=(
            "Which of A/D/F to run. Default: F only - a smoke-test default, not the canonical "
            "Phase 6.5 experiment. Pass `--configurations A D F` for that (see this script's "
            "module docstring)."
        ),
    )
    parser.add_argument(
        "--repeat", type=int, default=1,
        help=(
            "How many times to repeat each task. Default: 1 - a smoke-test default. The "
            "canonical Phase 6.5 experiment uses `--repeat 3` (see this script's module "
            "docstring), since a real model is not deterministic call-to-call."
        ),
    )
    parser.add_argument(
        "--model-id", default=None,
        help=(
            "Model ID/slug to pass to the provider (default: gemini-2.5-flash for gemini, "
            "gemini-3.8-flash-low for antigravity; inspect available agy slugs via `agy models`)."
        ),
    )
    parser.add_argument(
        "--min-call-interval-seconds", type=float, default=DEFAULT_MIN_CALL_INTERVAL_SECONDS,
        help=(
            f"Minimum seconds paced between real network attempts (default "
            f"{DEFAULT_MIN_CALL_INTERVAL_SECONDS}s - a safe margin above the observed Free Tier "
            "quota of 5 requests/minute/project/model). Ignored in dry-run mode, which makes no "
            "network calls at all."
        ),
    )
    parser.add_argument(
        "--max-retries", type=int, default=DEFAULT_MAX_RETRIES,
        help=(
            f"Maximum retries per real model call on retryable errors (429 RESOURCE_EXHAUSTED "
            f"or transient 5xx server errors) before giving up on that task/repeat (default {DEFAULT_MAX_RETRIES}). "
            "Ignored in dry-run mode."
        ),
    )
    parser.add_argument(
        "--retry-safety-margin-seconds", type=float, default=DEFAULT_RETRY_SAFETY_MARGIN_SECONDS,
        help=(
            f"Extra seconds added on top of max(server_delay, min_call_interval_seconds) when a "
            f"429 carries a server-provided retry delay (default {DEFAULT_RETRY_SAFETY_MARGIN_SECONDS}s) "
            "- a live run observed that delay reported as 0s on consecutive 429s within an "
            "already-saturated quota window, so it is a floor input now, not the sleep duration "
            "itself. Ignored in dry-run mode."
        ),
    )
    args = parser.parse_args()

    backend_dir = Path(__file__).resolve().parents[1]
    suite = load_benchmark(backend_dir / "benchmark" / "tasks_v1.json")
    subset = load_subset()
    provider = build_real_provider(
        provider_name=args.provider,
        model_id=args.model_id,
        min_call_interval_seconds=args.min_call_interval_seconds,
        max_retries=args.max_retries,
        retry_safety_margin_seconds=args.retry_safety_margin_seconds,
    )
    configurations = tuple(args.configurations)

    if not args.live:
        report = dry_run(suite=suite, subset=subset, configurations=configurations, repeat_count=args.repeat, provider=provider)
        print("DRY RUN - zero external calls made, no credentials required.")
        print(json.dumps(report.to_dict(), indent=2))
        for warning in report.warnings:
            print(f"\nWARNING: {warning}")
        return

    results_dir = backend_dir / "benchmark" / "real_model_results"
    outcome = live_run(
        suite=suite, subset=subset, results_dir=results_dir, configurations=configurations,
        repeat_count=args.repeat, real_provider=provider,
    )
    print(f"run_id: {outcome['run_id']}")
    print(f"results: {outcome['run_dir']}")


if __name__ == "__main__":
    main()
