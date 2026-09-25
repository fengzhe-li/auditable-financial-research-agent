"""Deterministic, pattern-based DLP scanning - docs/DATA_CLASSIFICATION.md's
"Sensitive content categories" section, implemented exactly as that document
says it should be: "explicit per-document/field classification, regex/pattern
matching, and known sensitive-field types" - deliberately NOT a
machine-learning or NER project. See that document's "Explicit non-guarantee"
section: this is a simple, auditable, low-recall pattern scan, not a claim of
production-grade PII detection.

`prompt_injection_pattern` is detected here too, but is handled as an
*audit signal only* (recorded on the resulting SecurityEvent, never causing a
block/redact/route_private by itself) - see the module docstring on
afra.policy.enforcement for why: the real defense against prompt injection in
this project is architectural (retrieved text is only ever passed as
untrusted data through the fixed-format deterministic test-double providers
in afra.providers.test_double, which parse fixed structural markers and
never "obey" instructions found in retrieved content, so injected text
cannot change support_status, routing, or trigger a tool call regardless of
whether this scan matches it). See docs/PHASE_4_REPORT.md's prompt-injection
example for the trace that demonstrates this directly.
"""

from __future__ import annotations

import re

_INJECTION_CATEGORY = "prompt_injection_pattern"

DLP_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone_number": re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),
    "ssn_like": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "account_number": re.compile(r"\bACCT-\d{4,}\b", re.IGNORECASE),
    "unpublished_financial_figure": re.compile(
        r"\bunpublished\b[^.]{0,40}\b(figures?|results?|numbers?)\b", re.IGNORECASE
    ),
    _INJECTION_CATEGORY: re.compile(
        r"ignore (all |any )?(previous|prior|the above) instructions"
        r"|disregard (all |any )?(previous|prior|the above) instructions"
        r"|mark this claim as supported",
        re.IGNORECASE,
    ),
}

# Every pattern-detected category except the injection signal is treated as
# sensitive content subject to routing/redaction decisions; the injection
# signal is informational only (see module docstring).
SENSITIVE_CATEGORIES = frozenset(DLP_PATTERNS) - {_INJECTION_CATEGORY}
REDACTABLE_CATEGORIES = SENSITIVE_CATEGORIES


def scan_for_sensitive_content(text: str) -> list[str]:
    """Returns every matched category name, deterministically ordered to
    match DLP_PATTERNS' insertion order. Includes `prompt_injection_pattern`
    when matched - callers that only care about routing-relevant sensitive
    content should filter it out via SENSITIVE_CATEGORIES.
    """
    return [category for category, pattern in DLP_PATTERNS.items() if pattern.search(text)]


def redact(text: str, categories: list[str]) -> str:
    """Masks every match of each named category with a fixed placeholder.
    Only categories in REDACTABLE_CATEGORIES have any effect - an unknown or
    non-redactable category name is silently ignored (the caller is
    responsible for deciding block/route_private for anything it didn't ask
    to redact).
    """
    redacted = text
    for category in categories:
        if category not in REDACTABLE_CATEGORIES:
            continue
        pattern = DLP_PATTERNS[category]
        redacted = pattern.sub(f"[REDACTED:{category.upper()}]", redacted)
    return redacted
